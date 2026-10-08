#   Copyright 2026 UCP Authors
#
#   Licensed under the Apache License, Version 2.0 (the "License");
#   you may not use this file except in compliance with the License.
#   You may obtain a copy of the License at
#
#       http://www.apache.org/licenses/LICENSE-2.0
#
#   Unless required by applicable law or agreed to in writing, software
#   distributed under the License is distributed on an "AS IS" BASIS,
#   WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#   See the License for the specific language governing permissions and
#   limitations under the License.

"""Checkout service for managing the lifecycle of checkout sessions.

This module provides the `CheckoutService` class, which encapsulates the
business logic
for creating, retrieving, updating, and completing checkout sessions. It handles
integration with the persistence layer, fulfillment calculation, payment
processing,
and inventory validation.

Key responsibilities include:
- Creating and managing checkout sessions with idempotency support.
- Calculating checkout totals, including line items, shipping, and discounts.
- Validating inventory availability.
- Processing payments via various handlers (e.g., Google Pay, Shop Pay, Mock).
- Transforming checkout sessions into completed orders.
- Supporting hierarchical fulfillment configuration.
"""

import asyncio
import datetime
import hashlib
import json
import logging
from typing import Any
import uuid

import config
import db
from enums import CheckoutStatus
from exceptions import CheckoutNotModifiableError
from exceptions import ConsentRequiredError
from exceptions import IdempotencyConflictError
from exceptions import InvalidRequestError
from exceptions import OutOfStockError
from exceptions import PaymentFailedError
from exceptions import ResourceNotFoundError
import httpx
from models import UnifiedCheckout as Checkout
from models import UnifiedCheckoutCreateRequest
from models import UnifiedCheckoutUpdateRequest
from pydantic import AnyUrl
from pydantic import BaseModel
from services import account_service
from services import booking_service
from services import coin_service
from services import ledger
from services import payment_rail
from services import voucher_service
from services.fulfillment_service import FulfillmentService
from sqlalchemy.ext.asyncio import AsyncSession
import ucp_signing
import webhook_signer
from ucp_sdk.models.schemas.ucp import (
  ResponseCheckoutSchema as ResponseCheckout,
)
from ucp_sdk.models.schemas.ucp import ResponseOrderSchema as ResponseOrder
from ucp_sdk.models.schemas.capability import ResponseSchema as Response
from ucp_sdk.models.schemas.shopping.checkout_complete_request import (
  CheckoutCompleteRequest,
)
from ucp_sdk.models.schemas.shopping.discount import Allocation
from ucp_sdk.models.schemas.shopping.discount import AppliedDiscount
from ucp_sdk.models.schemas.shopping.discount import DiscountsObject
from ucp_sdk.models.schemas.shopping.order import (
  Fulfillment as OrderFulfillment,
)
from ucp_sdk.models.schemas.shopping.order import Order
from ucp_sdk.models.schemas.shopping.order import PlatformSchema
from ucp_sdk.models.schemas.shopping.payment_create_request import (
  PaymentCreateRequest,
)
from ucp_sdk.models.schemas.shopping.payment import Payment as PaymentResponse
from ucp_sdk.models.schemas.shopping.types import order_line_item
from ucp_sdk.models.schemas.shopping.types.expectation import Expectation
from ucp_sdk.models.schemas.shopping.types.expectation import (
  LineItem as ExpectationLineItem,
)
from ucp_sdk.models.schemas.shopping.types.fulfillment_group import (
  FulfillmentGroup,
)
from ucp_sdk.models.schemas.shopping.types.fulfillment_method import (
  FulfillmentMethod,
)
from ucp_sdk.models.schemas.shopping.types.fulfillment import (
  Fulfillment as FulfillmentResponseClass,
)
from ucp_sdk.models.schemas.shopping.types.item import Item as ItemResponse
from ucp_sdk.models.schemas.shopping.types.line_item import (
  LineItem as LineItemResponse,
)
from ucp_sdk.models.schemas.shopping.types.order_confirmation import (
  OrderConfirmation,
)
from ucp_sdk.models.schemas.shopping.types.order_line_item import OrderLineItem
from ucp_sdk.models.schemas.shopping.types.postal_address import PostalAddress
from ucp_sdk.models.schemas.shopping.types.shipping_destination import (
  ShippingDestination as ShippingDestinationResponse,
)
from ucp_sdk.models.schemas.shopping.types.total import (
  Total as TotalResponse,
)

logger = logging.getLogger(__name__)


class CheckoutService:
  """Service for managing checkout sessions and orders."""

  def __init__(
    self,
    fulfillment_service: FulfillmentService,
    products_session: AsyncSession,
    transactions_session: AsyncSession,
    base_url: str,
  ):
    """Initialize CheckoutService."""
    self.fulfillment_service = fulfillment_service
    self.products_session = products_session
    self.transactions_session = transactions_session
    self.base_url = base_url.rstrip("/")

  def _compute_hash(
    self,
    operation: str,
    data: Any,
    resource_id: str | None = None,
  ) -> str:
    """Compute a hash of an idempotent operation's complete identity."""
    if isinstance(data, BaseModel):
      # Pydantic's optimized JSON dump
      # sort_keys is not supported in model_dump_json in Pydantic V2.
      # We dump to dict and use standard json.dumps for deterministic sorting.
      data = data.model_dump(mode="json")
    request_identity = {
      "operation": operation,
      "resource_id": resource_id,
      "data": data,
    }
    # sort_keys=True ensures deterministic hashing for dicts.
    json_str = json.dumps(request_identity, sort_keys=True)
    return hashlib.sha256(json_str.encode("utf-8")).hexdigest()

  async def create_checkout(
    self,
    checkout_req: UnifiedCheckoutCreateRequest,
    idempotency_key: str,
    platform_config: PlatformSchema | None = None,
  ) -> Checkout:
    """Create a new checkout session."""
    logger.info("Creating checkout session")

    # Idempotency Check
    request_hash = self._compute_hash("create_checkout", checkout_req)
    existing_record = await db.get_idempotency_record(
      self.transactions_session, idempotency_key
    )

    if existing_record:
      if existing_record.request_hash != request_hash:
        raise IdempotencyConflictError(
          "Idempotency key reused with different parameters"
        )
      # Return cached response
      return Checkout(**existing_record.response_body)
    cart_id = getattr(checkout_req, "cart_id", None)

    if cart_id:
      # Check if incomplete checkout already exists for this cart_id
      existing_checkouts = await db.get_checkouts_by_cart_id(
        self.transactions_session, cart_id
      )
      for data in existing_checkouts:
        if data.get("status") not in [
          CheckoutStatus.COMPLETED,
          CheckoutStatus.CANCELED,
        ]:
          logger.info(
            "Returning existing incomplete checkout for cart %s", cart_id
          )
          return Checkout(**data)

      # Load cart to initialize checkout
      cart_data = await db.get_cart_session(self.transactions_session, cart_id)
      if not cart_data:
        raise ResourceNotFoundError(f"Cart session {cart_id} not found")

      from models import UnifiedCart as CartModel

      cart = CartModel(**cart_data)

      # Initialize from cart
      source_line_items = cart.line_items
      source_buyer = cart.buyer
      source_context = cart.context
      source_signals = cart.signals
      source_attribution = cart.attribution
      source_currency = cart.currency
      source_discounts = cart.discounts
    else:
      # Initialize from request
      source_line_items = checkout_req.line_items
      source_buyer = checkout_req.buyer
      source_context = checkout_req.context
      source_signals = checkout_req.signals
      source_attribution = checkout_req.attribution
      # `currency` carries `ucp_request: omit`, so the merchant determines it.
      source_currency = config.get_default_currency()
      source_discounts = checkout_req.discounts

    # `id` carries `ucp_request: omit`, so the server assigns it and never
    # takes it from the request. The generated CheckoutCreateRequest declares
    # no id field, but extra="allow" admits a client-sent `id` of any JSON
    # type as an extra attribute; reading it here propagated that raw value
    # into the response model, where a non-string raised an uncaught
    # ValidationError. Same defect class as the currency read fixed in #156.
    checkout_id = str(uuid.uuid4())

    # Map line items
    line_items = []
    for li in source_line_items:
      item_id = li.item.id
      quantity = li.quantity
      parent_id = getattr(li, "parent_id", None)
      # When converting from a cart, preserve the cart line item id.
      # On direct create, line item `id` carries `create: omit` so
      # the server assigns it.
      li_id = li.id if cart_id else str(uuid.uuid4())
      line_items.append(
        LineItemResponse(
          id=li_id,
          item=ItemResponse(
            id=item_id,
            title="",
            price=0,  # Will be set by recalculate_totals
          ),
          quantity=quantity,
          totals=[],
          parent_id=parent_id,
        )
      )

    # We exclude fields that the service explicitly manages or overrides, as
    # well as fields marked as `ucp_request: omit` in checkout.json
    # (continue_url, expires_at, messages, order) to ensure the server is the
    # authoritative source and client values do not bleed into the response.
    #
    # * Conflict Prevention: If we didn't exclude currency, id, or payment,
    #   passing them via **checkout_data while also specifying them as keyword
    #   arguments (e.g., currency=checkout_req.currency) would raise a
    #   TypeError: multiple values for keyword argument.
    # * Server Authority: Fields like status, totals, links, continue_url,
    #   expires_at, messages, and order are merchant-owned. We exclude them from
    #   the dumped data to ensure we start with a clean calculated state and
    #   client-supplied omit members are dropped.
    # * Model Transformation: ucp in the request is usually just version
    #   negotiation info, but in the response, it's a complex ResponseCheckout
    #   object with capability metadata. We exclude the request version to
    #   inject the full response object.
    checkout_data = checkout_req.model_dump(
      exclude={
        "line_items",
        "payment",
        "ucp",
        "currency",
        "id",
        "status",
        "totals",
        "links",
        "fulfillment",
        "buyer",
        "context",
        "signals",
        "attribution",
        "cart_id",
        "discounts",
        "continue_url",
        "expires_at",
        "messages",
        "order",
      }
    )

    # Initialize fulfillment response
    fulfillment_resp = None
    if checkout_req.fulfillment:
      req_fulfillment = checkout_req.fulfillment
      resp_methods = []
      all_li_ids = [li.id for li in line_items]

      if req_fulfillment.methods:
        for method_req in req_fulfillment.methods:
          # Create Method Response
          method_id = getattr(method_req, "id", None) or str(uuid.uuid4())
          method_li_ids = (
            getattr(method_req, "line_item_ids", None) or all_li_ids
          )
          method_type = getattr(method_req, "type", "shipping")

          resp_groups = []
          if method_req.groups:
            for group_req in method_req.groups:
              group_id = (
                getattr(group_req, "id", None) or f"group_{uuid.uuid4()}"
              )
              group_li_ids = (
                getattr(group_req, "line_item_ids", None) or all_li_ids
              )

              resp_groups.append(
                FulfillmentGroup(
                  id=group_id,
                  line_item_ids=group_li_ids,
                  selected_option_id=getattr(
                    group_req, "selected_option_id", None
                  ),
                )
              )

          # Convert destinations if present (usually empty on create, but
          # handled for completeness)
          resp_destinations = []
          if method_req.destinations:
            for dest_req in method_req.destinations:
              # Assuming ShippingDestinationRequest can map to Response
              # structure or needs conversion. For create, we typically accept
              # ShippingDestinationRequest inside
              # FulfillmentMethodCreateRequest. We need to convert it to
              # FulfillmentDestinationResponse.
              # The request model structure is complex
              # (FulfillmentDestinationRequest -> ShippingDestinationRequest)
              # The response model is FulfillmentDestinationResponse ->
              # ShippingDestinationResponse

              resp_destinations.append(
                ShippingDestinationResponse(
                  id=getattr(dest_req, "id", None) or str(uuid.uuid4()),
                  address_country=dest_req.address_country,
                  postal_code=dest_req.postal_code,
                  address_region=dest_req.address_region,
                  address_locality=dest_req.address_locality,
                  street_address=dest_req.street_address,
                )
              )

          resp_methods.append(
            FulfillmentMethod(
              id=method_id,
              type=method_type,
              line_item_ids=method_li_ids,
              groups=resp_groups or None,
              destinations=resp_destinations or None,
              selected_destination_id=getattr(
                method_req, "selected_destination_id", None
              ),
            )
          )

      fulfillment_resp = FulfillmentResponseClass(methods=resp_methods)

    # The SDK enforces the totals contains cardinality (exactly one subtotal
    # and one total) on the response model. The server is the authority for
    # totals and computes them in _recalculate_totals below, but that runs
    # after construction. Seed a valid placeholder so the in-progress model
    # satisfies the constraint at construction; it is overwritten with the
    # authoritative amounts immediately. See python-sdk#57.
    checkout = Checkout(
      ucp=ResponseCheckout(
        version=config.get_server_version(),
        capabilities={
          "dev.ucp.shopping.checkout": [
            Response(
              name="dev.ucp.shopping.checkout",
              version=config.get_server_version(),
            )
          ]
        },
        payment_handlers=config.get_payment_handlers(),
      ),
      id=checkout_id,
      status=CheckoutStatus.IN_PROGRESS,
      currency=source_currency,
      line_items=line_items,
      totals=[
        {"type": "subtotal", "amount": 0},
        {"type": "total", "amount": 0},
      ],
      links=[],
      payment=PaymentResponse(
        instruments=checkout_req.payment.instruments
        if checkout_req.payment
        else None,
      )
      if checkout_req.payment
      else None,
      platform=platform_config,
      fulfillment=fulfillment_resp,
      buyer=source_buyer.model_dump(exclude_none=True)
      if source_buyer
      else None,
      context=source_context.model_dump(exclude_none=True)
      if source_context
      else None,
      signals=source_signals.model_dump(exclude_none=True)
      if source_signals
      else None,
      attribution=dict(source_attribution) if source_attribution else None,
      cart_id=cart_id,
      discounts=source_discounts.model_dump(exclude_none=True)
      if source_discounts
      else None,
      **checkout_data,
    )

    # Validate inventory and recalculate totals (Server is authority)
    await self._enrich_and_recalculate(checkout)
    await self._validate_inventory(checkout)
    await voucher_service.check_purchase_limits(
      self.products_session,
      self.transactions_session,
      checkout,
      count_history=False,
    )

    checkout.status = CheckoutStatus.READY_FOR_COMPLETE

    response_body = checkout.model_dump(
      mode="json", by_alias=True, exclude_none=True
    )

    # Persist checkout to Transactions DB
    await db.save_checkout(
      self.transactions_session,
      checkout.id,
      checkout.status,
      response_body,
    )

    # Save Idempotency Record
    await db.save_idempotency_record(
      self.transactions_session,
      idempotency_key,
      request_hash,
      201,  # Created
      response_body,
    )

    await self.transactions_session.commit()
    ledger.emit(
      "CHECKOUT_CREATED",
      checkout_id=checkout.id,
      detail={"total": self._total_amount(checkout)},
    )

    return checkout

  async def get_checkout(
    self,
    checkout_id: str,
  ) -> Checkout:
    """Retrieve a checkout session."""
    # Log the request
    await db.log_request(
      self.transactions_session,
      method="GET",
      url=f"/checkout-sessions/{checkout_id}",
      checkout_id=checkout_id,
    )
    await self.transactions_session.commit()

    return await self._get_and_validate_checkout(checkout_id)

  async def update_checkout(
    self,
    checkout_id: str,
    checkout_req: UnifiedCheckoutUpdateRequest,
    idempotency_key: str,
    platform_config: PlatformSchema | None = None,
  ) -> Checkout:
    """Update a checkout session."""
    logger.info("Updating checkout session %s", checkout_id)

    # Idempotency Check
    request_hash = self._compute_hash(
      "update_checkout", checkout_req, resource_id=checkout_id
    )

    existing_record = await db.get_idempotency_record(
      self.transactions_session, idempotency_key
    )
    if existing_record:
      if existing_record.request_hash != request_hash:
        raise IdempotencyConflictError(
          "Idempotency key reused with different parameters"
        )
      return Checkout(**existing_record.response_body)

    # Log the request
    payload_dict = checkout_req.model_dump(mode="json")
    await db.log_request(
      self.transactions_session,
      method="PUT",
      url=f"/checkout-sessions/{checkout_id}",
      checkout_id=checkout_id,
      payload=payload_dict,
    )

    existing = await self._get_and_validate_checkout(checkout_id)
    self._ensure_modifiable(existing, "update")

    # Update existing with request data
    # This is a partial update logic
    if checkout_req.line_items:
      line_items = []
      for li_req in checkout_req.line_items:
        line_items.append(
          LineItemResponse(
            id=li_req.id or str(uuid.uuid4()),
            item=ItemResponse(
              id=li_req.item.id,
              title="",
              price=0,
            ),
            quantity=li_req.quantity,
            totals=[],
            parent_id=li_req.parent_id,
          )
        )
      existing.line_items = line_items

    # `currency` carries `ucp_request: omit`, so the business determines it
    # and an update never takes it from the request. Reading it here also
    # raised AttributeError whenever a conformant platform omitted it, which
    # is the same defect as the create path.

    if checkout_req.payment:
      existing.payment = PaymentResponse(
        instruments=checkout_req.payment.instruments,
      )

    if checkout_req.buyer:
      existing.buyer = checkout_req.buyer

    if hasattr(checkout_req, "fulfillment") and checkout_req.fulfillment:
      # Hierarchical fulfillment update
      logging.info(
        "Processing hierarchical fulfillment update for %s", checkout_id
      )

      # Fetch customer addresses if buyer is known
      customer_addresses = []
      if existing.buyer and existing.buyer.email:
        customer_addresses = await db.get_customer_addresses(
          self.transactions_session, existing.buyer.email
        )

      req_fulfillment = checkout_req.fulfillment
      resp_methods = []

      if req_fulfillment.methods:
        logging.info("Request has %d methods", len(req_fulfillment.methods))
        for m_req in req_fulfillment.methods:
          # Find matching existing method to preserve state
          existing_method = None
          if existing.fulfillment and existing.fulfillment.methods:
            existing_method = next(
              (
                m
                for m in existing.fulfillment.methods
                if m.id == getattr(m_req, "id", None)
              ),
              None,
            )
            # Fallback: If no ID in request, and only 1 existing method, match
            # it
            if (
              not existing_method
              and not getattr(m_req, "id", None)
              and len(existing.fulfillment.methods) == 1
            ):
              existing_method = existing.fulfillment.methods[0]

          # Resolve ID
          method_id = getattr(m_req, "id", None)
          if existing_method and not method_id:
            method_id = existing_method.id
          if not method_id:
            method_id = str(uuid.uuid4())

          method_type = getattr(m_req, "type", "shipping")
          method_li_ids = getattr(m_req, "line_item_ids", None) or [
            li.id for li in existing.line_items
          ]

          resp_destinations = []

          # Handle destinations
          if method_type == "shipping":
            if m_req.destinations:
              # Use provided destinations
              for dest_req in m_req.destinations:
                dest_data = dest_req.model_dump(exclude_none=True)

                # Persist addresses for known customers
                if existing.buyer and existing.buyer.email:
                  # Save and update ID
                  saved_id = await db.save_customer_address(
                    self.transactions_session,
                    existing.buyer.email,
                    dest_data,
                  )
                  dest_data["id"] = saved_id

                resp_destinations.append(
                  ShippingDestinationResponse(**dest_data)
                )

            elif existing_method and existing_method.destinations:
              # Preserve existing destinations
              resp_destinations = existing_method.destinations
            elif customer_addresses:
              for addr in customer_addresses:
                resp_destinations.append(
                  ShippingDestinationResponse(
                    id=addr.id,
                    street_address=addr.street_address,
                    address_locality=addr.city,
                    address_region=addr.state,  # Map state to region
                    postal_code=addr.postal_code,
                    address_country=addr.country,
                  )
                )

          # Handle groups
          resp_groups = []
          if m_req.groups:
            for g_req in m_req.groups:
              g_id = getattr(g_req, "id", None) or f"group_{uuid.uuid4()}"
              g_li_ids = getattr(g_req, "line_item_ids", None) or [
                li.id for li in existing.line_items
              ]
              resp_groups.append(
                FulfillmentGroup(
                  id=g_id,
                  line_item_ids=g_li_ids,
                  selected_option_id=getattr(g_req, "selected_option_id", None),
                )
              )
          elif existing_method and existing_method.groups:
            # Preserve existing groups if not updating them
            resp_groups = existing_method.groups

          # Construct the method response
          method_resp = FulfillmentMethod(
            id=method_id,
            type=method_type,
            line_item_ids=method_li_ids,
            groups=resp_groups or None,
            destinations=resp_destinations or None,
            selected_destination_id=getattr(
              m_req, "selected_destination_id", None
            ),
          )
          resp_methods.append(method_resp)

      existing.fulfillment = FulfillmentResponseClass(
        methods=resp_methods,
      )

    if getattr(checkout_req, "discounts", None):
      existing.discounts = checkout_req.discounts

    if getattr(checkout_req, "coins", None) is not None:
      existing.coins = checkout_req.coins
    if getattr(checkout_req, "bookings", None) is not None:
      # One slot per deal line, by item id; an empty value drops the slot.
      slots = {**(getattr(existing, "bookings", None) or {})}
      slots.update(checkout_req.bookings or {})
      existing.bookings = {k: v for k, v in slots.items() if v}

    if platform_config:
      existing.platform = platform_config

    # Validate inventory and recalculate totals (Server is authority)
    await self._enrich_and_recalculate(existing)
    await self._validate_inventory(existing)
    await voucher_service.check_purchase_limits(
      self.products_session,
      self.transactions_session,
      existing,
      count_history=False,
    )

    response_body = existing.model_dump(
      mode="json", by_alias=True, exclude_none=True
    )

    await db.save_checkout(
      self.transactions_session,
      checkout_id,
      existing.status,
      response_body,
    )

    # Save Idempotency Record
    await db.save_idempotency_record(
      self.transactions_session,
      idempotency_key,
      request_hash,
      200,
      response_body,
    )

    await self.transactions_session.commit()
    return existing

  async def complete_checkout(
    self,
    checkout_id: str,
    payment: PaymentCreateRequest,
    risk_signals: dict[str, Any],
    idempotency_key: str,
    checkout_complete: CheckoutCompleteRequest | None = None,
    channel: str = "agent",
    agent: str | None = None,
    visit: dict[str, Any] | None = None,
    signature: dict[str, Any] | None = None,
    agent_context: dict[str, Any] | None = None,
  ) -> Checkout:
    """Complete a checkout session.

    `channel` is the door the buyer came through: 'agent' (UCP, with the
    agent's profile in `agent` and what the shop found out about its request
    signature in `signature`) or 'web' (with what the request told about its
    sender in `visit`; see services/visitor.py). An agent may also say in
    `agent_context` how its customer came to the purchase: who proposed it,
    through which channel the yes came and when, and where its records are.
    The shop keeps it on the order, as told, for the customer to see.
    """
    logger.info("Completing checkout session %s", checkout_id)

    # Idempotency Check
    # Include risk_signals and checkout_complete in the hash
    combined_data = {
      "payment": payment.model_dump(mode="json"),
      "risk_signals": risk_signals,
      "checkout_complete": checkout_complete.model_dump(mode="json")
      if checkout_complete
      else None,
    }
    request_hash = self._compute_hash(
      "complete_checkout", combined_data, resource_id=checkout_id
    )

    existing_record = await db.get_idempotency_record(
      self.transactions_session, idempotency_key
    )
    if existing_record:
      if existing_record.request_hash != request_hash:
        raise IdempotencyConflictError(
          "Idempotency key reused with different parameters"
        )
      return Checkout(**existing_record.response_body)

    # Log the request
    await db.log_request(
      self.transactions_session,
      method="POST",
      url=f"/checkout-sessions/{checkout_id}/complete",
      checkout_id=checkout_id,
      payload=combined_data,
    )

    checkout = await self._get_and_validate_checkout(checkout_id)
    self._ensure_modifiable(checkout, "complete")
    await self._ensure_unchanged(checkout)
    await voucher_service.check_purchase_limits(
      self.products_session,
      self.transactions_session,
      checkout,
      count_history=True,
    )
    # A deal booked at purchase needs its slot, with room, before any charge.
    await booking_service.ensure(
      self.products_session, self.transactions_session, checkout
    )

    # What the card pays, after any coins. Nothing, if they cover it all.
    total = self._total_amount(checkout)
    coins_spent = coin_service.applied(checkout)

    def declined(refusal: PaymentFailedError) -> None:
      # A decline is worth a line: the console counts them.
      ledger.emit(
        "PAYMENT_DECLINED",
        checkout_id=checkout.id,
        detail={
          "total": total,
          "code": refusal.code,
          "message": refusal.message,
          "channel": channel,
          "agent": agent,
        },
      )

    # Check the payment instrument. The rail gets its credential below.
    credential_token = None
    if total:
      try:
        credential_token = await self._process_payment(payment)
      except PaymentFailedError as refusal:
        declined(refusal)
        raise

    # Validate Fulfillment (Required for completion in this implementation)
    fulfillment_valid = False
    if checkout.fulfillment and checkout.fulfillment.methods:
      for method in checkout.fulfillment.methods:
        if method.type == "shipping" and not method.selected_destination_id:
          continue
        if method.groups:
          for group in method.groups:
            if group.selected_option_id:
              fulfillment_valid = True
              break
        if fulfillment_valid:
          break

    # Vouchers are delivered with the order, so there is nothing to ship.
    is_voucher_checkout = await voucher_service.is_voucher_checkout(
      self.products_session, checkout
    )

    if not fulfillment_valid and not is_voucher_checkout:
      raise InvalidRequestError(
        "Fulfillment address and option must be selected before completion."
      )

    # Lock the total on the payment rail. It is captured once the order is
    # built, and given back if anything below fails.
    rail = payment_rail.get_rail()
    payment_id = None
    if total:
      try:
        payment_id = await rail.lock(
          total, checkout.currency, checkout.id, credential_token
        )
      except PaymentFailedError as refusal:
        declined(refusal)
        raise

    # Atomic Inventory Reservation + Order Completion
    try:
      for line in checkout.line_items:
        product_id = line.item.id
        # We verify product existence again (optional but good practice)
        if await db.get_product(self.products_session, product_id):
          success = await db.reserve_stock(
            self.transactions_session, product_id, line.quantity
          )
          if not success:
            # This rollback applies to the transaction_session
            await self.transactions_session.rollback()
            raise OutOfStockError(
              f"Item {product_id} is out of stock", status_code=409
            )

      checkout.status = CheckoutStatus.COMPLETED
      order_id = f"{uuid.uuid4()}"
      order_permalink_url = AnyUrl(f"{self.base_url}/orders/{order_id}")

      checkout.order = OrderConfirmation(
        id=order_id, permalink_url=order_permalink_url
      )
      response_body = checkout.model_dump(
        mode="json", by_alias=True, exclude_none=True
      )

      # Create and persist Order
      expectations = []
      if checkout.fulfillment and checkout.fulfillment.methods:
        for method in checkout.fulfillment.methods:
          selected_dest = None
          if method.selected_destination_id and method.destinations:
            for dest in method.destinations:
              if dest.id == method.selected_destination_id:
                selected_dest = PostalAddress(
                  street_address=dest.street_address,
                  address_locality=dest.address_locality,
                  address_region=dest.address_region,
                  postal_code=dest.postal_code,
                  address_country=dest.address_country,
                )
                break

          if method.groups:
            for group in method.groups:
              if getattr(group, "selected_option_id", None):
                options = getattr(group, "options", [])
                selected_opt = next(
                  (
                    o
                    for o in (options or [])
                    if o.id == group.selected_option_id
                  ),
                  None,
                )
                expectation_id = f"exp_{uuid.uuid4()}"

                exp_line_items = []
                for li in checkout.line_items:
                  if li.id in getattr(group, "line_item_ids", []):
                    exp_line_items.append(
                      ExpectationLineItem(id=li.id, quantity=li.quantity)
                    )
                # Fallback to all line items if none match
                # (e.g. tests using item_123)
                if not exp_line_items:
                  for li in checkout.line_items:
                    exp_line_items.append(
                      ExpectationLineItem(id=li.id, quantity=li.quantity)
                    )

                expectations.append(
                  Expectation(
                    id=expectation_id,
                    line_items=exp_line_items,
                    method_type=method.type,
                    destination=selected_dest,
                    description=getattr(
                      selected_opt, "title", "Standard Shipping"
                    ),
                  )
                )

      order_line_items = []

      for li in checkout.line_items:
        # Create Quantity object for OrderLineItem
        qty = order_line_item.Quantity(total=li.quantity, fulfilled=0)

        oli = OrderLineItem(
          id=li.id,
          item=li.item,
          quantity=qty,
          totals=li.totals,
          status="processing",
          parent_id=li.parent_id,
        )
        order_line_items.append(oli)

      order = Order(
        ucp=ResponseOrder(
          version=getattr(checkout.ucp, "version", "2026-04-08"),
          capabilities=dict(checkout.ucp.capabilities)
          if hasattr(checkout.ucp, "capabilities") and checkout.ucp.capabilities
          else {},
        ),
        id=checkout.order.id,
        checkout_id=checkout.id,
        permalink_url=checkout.order.permalink_url,
        line_items=order_line_items,
        totals=checkout.totals,
        currency=checkout.currency,
        fulfillment=OrderFulfillment(expectations=expectations, events=[]),
      )

      order_data = order.model_dump(mode="json", by_alias=True)
      voucher_codes = await voucher_service.issue_vouchers(
        self.products_session,
        self.transactions_session,
        order_data,
        buyer_email=getattr(checkout.buyer, "email", None),
      )
      # The slots the buyer chose, taken now and written on the order.
      await booking_service.book(
        self.products_session,
        self.transactions_session,
        checkout,
        order_data,
        buyer_email=getattr(checkout.buyer, "email", None),
      )
      if payment_id:
        await rail.capture(payment_id)
      buyer_email = getattr(checkout.buyer, "email", None)
      coins_earned = await coin_service.settle(
        self.transactions_session,
        order_id,
        buyer_email and str(buyer_email),
        coins_spent,
        total,
      )
      # The split the buyer approved: `amount` by card, `coins` from the
      # wallet. A refund sends each part back where it came from.
      order_data["payment"] = {
        "rail": rail.name,
        "payment_id": payment_id,
        "status": "captured",
        "amount": total,
        "currency": checkout.currency,
        "coins": coins_spent,
        "coins_earned": coins_earned,
      }
      order_data["buyer"] = {
        "email": str(buyer_email).lower() if buyer_email else None,
        "full_name": getattr(checkout.buyer, "full_name", None),
      }
      order_data["channel"] = channel
      order_data["agent"] = agent
      order_data["placed_at"] = datetime.datetime.now(
        datetime.timezone.utc
      ).isoformat()
      if visit:
        order_data["visit"] = visit
      if agent_context:
        order_data["agent_context"] = agent_context
      if signature:
        order_data["signature"] = signature

      await db.save_order(self.transactions_session, order.id, order_data)
      # The buyer finds the order in their account, whoever placed it.
      await account_service.link_order(
        self.transactions_session, order.id, buyer_email
      )

      await db.save_checkout(
        self.transactions_session,
        checkout_id,
        checkout.status,
        response_body,
      )

      # Save Idempotency Record
      await db.save_idempotency_record(
        self.transactions_session,
        idempotency_key,
        request_hash,
        200,
        response_body,
      )

      if checkout.cart_id:
        logger.info(
          "Clearing cart %s after checkout completion", checkout.cart_id
        )
        await db.delete_cart_session(
          self.transactions_session, checkout.cart_id
        )

      # Commit both inventory updates and checkout status update atomically
      await self.transactions_session.commit()

    except Exception as e:
      await self.transactions_session.rollback()
      if payment_id:
        await rail.refund(payment_id, total)
      raise e

    ledger.emit(
      "PAYMENT_CONFIRMED",
      checkout_id=checkout.id,
      payment_id=payment_id,
      detail={
        "order_id": order_id,
        "amount": total,
        "coins": coins_spent,
        "channel": channel,
        "agent": agent,
        "visit": visit,
        "signature": signature and signature["status"],
        "agent_context": agent_context,
      },
    )
    if voucher_codes:
      ledger.emit(
        "VOUCHER_ISSUED",
        checkout_id=checkout.id,
        payment_id=payment_id,
        detail={"order_id": order_id, "vouchers": len(voucher_codes)},
      )

    # Notify webhook of order placement
    await self._notify_webhook(checkout, "order_placed")

    return checkout

  async def _notify_webhook(self, checkout: Checkout, event_type: str) -> None:
    """Notifies the configured webhook of an order event.

    Per the UCP REST OpenAPI (``webhooks.orderEvent``), the request body is the
    order object itself (``#/components/schemas/order``). This sample also sends
    an ``X-Event-Type`` extension header, but it is not part of the UCP webhook
    contract and conforming receivers cannot require it. The body must always be
    a valid order, so no notification is sent when there is no order to deliver.

    Every delivery is RFC 9421-signed as this business (order.md, Webhook
    Signature Verification): ``UCP-Agent`` names our profile, and
    ``Content-Digest``/``Signature-Input``/``Signature`` cover the exact raw
    body bytes sent on the wire. Failed deliveries (transport errors or a
    5xx from the receiver) are retried with bounded exponential backoff
    (order.md: businesses MUST retry failed webhook deliveries); a 4xx is a
    permanent rejection and is not retried. Delivery failures are logged and
    never propagate into the order flow.
    """
    if not checkout.platform or not checkout.platform.webhook_url:
      return

    order_data = None
    if checkout.order and checkout.order.id:
      order_data = await db.get_order(
        self.transactions_session, checkout.order.id
      )

    if not order_data:
      logger.warning(
        "Skipping %s webhook for checkout %s: no order to deliver",
        event_type,
        checkout.id,
      )
      return

    webhook_url = str(checkout.platform.webhook_url)
    # Serialize exactly once: the signed Content-Digest and the wire body
    # must be the same bytes.
    body = json.dumps(order_data).encode("utf-8")
    webhook_id = str(uuid.uuid4())
    headers = {
      "Content-Type": "application/json",
      "X-Event-Type": event_type,
      "Webhook-Id": webhook_id,
      "Webhook-Timestamp": str(
        int(datetime.datetime.now(datetime.timezone.utc).timestamp())
      ),
      # A webhook POST is a state-changing request, so the signed-component
      # table requires idempotency-key (signatures.md). The event id doubles
      # as the key: retries carry the same value, letting the platform
      # deduplicate redelivered events.
      "Idempotency-Key": webhook_id,
      # Sign AS this business: the profile URL platforms fetch our
      # signing_keys[] from (order.md requires UCP-Agent on deliveries).
      "UCP-Agent": f'profile="{self.base_url}/.well-known/ucp"',
    }
    attempts = config.FLAGS.webhook_delivery_attempts
    backoff = config.FLAGS.webhook_retry_backoff_seconds
    try:
      key, kid = webhook_signer.signing_key()
      async with httpx.AsyncClient() as client:
        for attempt in range(1, attempts + 1):
          # Re-sign per attempt so the signature's `created` timestamp
          # reflects the actual send time of each delivery attempt.
          additions = ucp_signing.sign_request(
            key,
            kid,
            "POST",
            webhook_url,
            headers,
            body,
            extra_components=(
              "webhook-id",
              "webhook-timestamp",
              "x-event-type",
            ),
          )
          try:
            response = await client.post(
              webhook_url,
              content=body,
              headers={**headers, **additions},
              timeout=5.0,
            )
          except httpx.HTTPError as exc:
            failure = f"transport error: {exc}"
          else:
            if 200 <= response.status_code < 300:
              return
            failure = f"HTTP {response.status_code}"
            if response.status_code < 500:
              # The receiver rejected this delivery as invalid; retrying
              # the same request cannot succeed.
              logger.error(
                "Webhook delivery to %s permanently rejected (%s); "
                "not retrying",
                webhook_url,
                failure,
              )
              return
          if attempt < attempts:
            delay = backoff * (2 ** (attempt - 1))
            logger.warning(
              "Webhook delivery attempt %d/%d to %s failed (%s); "
              "retrying in %.2fs",
              attempt,
              attempts,
              webhook_url,
              failure,
              delay,
            )
            await asyncio.sleep(delay)
        logger.error(
          "Failed to deliver %s webhook to %s after %d attempts (%s)",
          event_type,
          webhook_url,
          attempts,
          failure,
        )
    except Exception as e:  # pylint: disable=broad-exception-caught
      logger.error("Failed to notify webhook at %s: %s", webhook_url, e)

  async def ship_order(self, order_id: str) -> None:
    """Simulate shipping an order and notifies the webhook."""
    order_data = await db.get_order(self.transactions_session, order_id)
    if not order_data:
      raise ResourceNotFoundError("Order not found")

    # Add shipping event to order
    if "fulfillment" not in order_data:
      order_data["fulfillment"] = {"events": []}
    if (
      "events" not in order_data["fulfillment"]
      or order_data["fulfillment"]["events"] is None
    ):
      order_data["fulfillment"]["events"] = []

    event_id = f"evt_{uuid.uuid4()}"
    occurred_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
    line_items = [
      {
        "id": line_item["id"],
        "quantity": line_item["quantity"]["total"],
      }
      for line_item in order_data["line_items"]
    ]

    order_data["fulfillment"]["events"].append(
      {
        "id": event_id,
        "type": "shipped",
        "occurred_at": occurred_at,
        "line_items": line_items,
      }
    )

    await db.save_order(self.transactions_session, order_id, order_data)
    await self.transactions_session.commit()

    # Get checkout to find webhook_url
    checkout_id = order_data.get("checkout_id")
    if checkout_id:
      checkout = await self._get_and_validate_checkout(checkout_id)
      await self._notify_webhook(checkout, "order_shipped")

  async def cancel_checkout(
    self,
    checkout_id: str,
    idempotency_key: str,
  ) -> Checkout:
    """Cancel a checkout session."""
    logger.info("Canceling checkout session %s", checkout_id)

    # Idempotency Check
    # Payload is empty for cancel usually.
    request_hash = self._compute_hash(
      "cancel_checkout", {}, resource_id=checkout_id
    )

    existing_record = await db.get_idempotency_record(
      self.transactions_session, idempotency_key
    )
    if existing_record:
      if existing_record.request_hash != request_hash:
        raise IdempotencyConflictError(
          "Idempotency key reused with different parameters"
        )
      return Checkout(**existing_record.response_body)

    # Log the request
    await db.log_request(
      self.transactions_session,
      method="POST",
      url=f"/checkout-sessions/{checkout_id}/cancel",
      checkout_id=checkout_id,
    )

    checkout = await self._get_and_validate_checkout(checkout_id)
    self._ensure_modifiable(checkout, "cancel")

    checkout.status = CheckoutStatus.CANCELED
    response_body = checkout.model_dump(
      mode="json", by_alias=True, exclude_none=True
    )

    await db.save_checkout(
      self.transactions_session,
      checkout_id,
      checkout.status,
      response_body,
    )

    # Save Idempotency Record
    await db.save_idempotency_record(
      self.transactions_session,
      idempotency_key,
      request_hash,
      200,
      response_body,
    )

    await self.transactions_session.commit()
    return checkout

  async def get_order(
    self,
    order_id: str,
  ) -> dict[str, Any]:
    """Retrieve an order."""
    data = await db.get_order(self.transactions_session, order_id)
    if not data:
      raise ResourceNotFoundError("Order not found")
    return data

  async def update_order(
    self,
    order_id: str,
    order: dict[str, Any],
  ) -> dict[str, Any]:
    """Update an order."""
    # Verify existence
    await self.get_order(order_id)

    # Persist
    await db.save_order(
      self.transactions_session,
      order_id,
      order,
    )
    await self.transactions_session.commit()
    return order

  async def _get_and_validate_checkout(self, checkout_id: str) -> Checkout:
    """Retrieve a checkout session and validates its existence."""
    data = await db.get_checkout_session(self.transactions_session, checkout_id)
    if not data:
      raise ResourceNotFoundError("Checkout session not found")
    return Checkout(**data)

  def _ensure_modifiable(self, checkout: Checkout, action: str) -> None:
    """Ensure that the checkout is in a state that allows modification."""
    if checkout.status in [CheckoutStatus.COMPLETED, CheckoutStatus.CANCELED]:
      raise CheckoutNotModifiableError(
        f"Cannot {action} checkout in state '{checkout.status}'"
      )

  def _total_amount(self, checkout: Checkout) -> int:
    """Return the checkout's grand total in cents."""
    return next(t.amount for t in checkout.totals if t.type == "total")

  async def _ensure_unchanged(self, checkout: Checkout) -> None:
    """Refuse to complete a checkout whose total moved since it was last sent.

    The buyer approved the total the platform last saw. If the shop's prices
    or fees changed since, the new total is saved for the platform to fetch
    and show the buyer again, and nothing is charged.
    """
    # Recalculate on a copy, so an unchanged checkout completes exactly as it
    # was loaded.
    current = checkout.model_copy(deep=True)
    await self._enrich_and_recalculate(current)
    seen_total = self._total_amount(checkout)
    new_total = self._total_amount(current)
    if new_total == seen_total:
      return

    await db.save_checkout(
      self.transactions_session,
      current.id,
      current.status,
      current.model_dump(mode="json", by_alias=True, exclude_none=True),
    )
    await self.transactions_session.commit()
    ledger.emit(
      "CHECKOUT_CHANGED",
      checkout_id=checkout.id,
      detail={"approved_total": seen_total, "new_total": new_total},
    )
    raise ConsentRequiredError(
      f"The total changed from {seen_total} to {new_total} after it was"
      " shown to the buyer. Fetch the checkout and ask the buyer again."
    )

  async def _validate_inventory(
    self,
    checkout: Checkout,
  ) -> None:
    """Validate that all items in the checkout have sufficient stock."""
    for line in checkout.line_items:
      product_id = line.item.id
      qty_avail = await db.get_inventory(self.transactions_session, product_id)
      if qty_avail is None or qty_avail < line.quantity:
        raise OutOfStockError(f"Insufficient stock for item {product_id}")

  async def _enrich_and_recalculate(
    self,
    checkout: Checkout,
  ) -> None:
    """Enrich items from catalog and recalculate totals."""
    grand_total = 0

    for line in checkout.line_items:
      product_id = line.item.id
      product = await db.get_product(self.products_session, product_id)
      if not product:
        raise InvalidRequestError(f"Product {product_id} not found")

      # Use authoritative price and title from DB
      line.item.price = product.price
      line.item.title = product.title
      await voucher_service.attach_terms(self.products_session, line)

      base_amount = product.price * line.quantity
      line.totals = [
        TotalResponse(type="subtotal", amount=base_amount),
        TotalResponse(type="total", amount=base_amount),
      ]
      grand_total += base_amount

    checkout.totals = []
    # Always include subtotal for clarity when other costs might be added
    checkout.totals.append(TotalResponse(type="subtotal", amount=grand_total))

    booking_fee = await voucher_service.booking_fee(self.transactions_session)
    if booking_fee:
      grand_total += booking_fee
      checkout.totals.append(
        TotalResponse(
          type="fee", display_text="Booking fee", amount=booking_fee
        )
      )

    # Fulfillment Logic
    if checkout.fulfillment and checkout.fulfillment.methods:
      # Fetch promotions once for the loop
      promotions = await db.get_active_promotions(self.products_session)

      for method in checkout.fulfillment.methods:
        # 1. Identify Destination and Calculate Options
        calculated_options = []
        if method.type == "shipping" and method.selected_destination_id:
          selected_dest = None
          if method.destinations:
            for dest in method.destinations:
              if dest.id == method.selected_destination_id:
                selected_dest = dest
                break

          if selected_dest:
            logger.info(
              "Calculating options for country: %s (dest_id: %s)",
              selected_dest.address_country,
              method.selected_destination_id,
            )
            # Log all available destinations for debugging
            if method.destinations:
              logger.info(
                "Available destinations in method %s: %s",
                method.id,
                [f"{d.id} ({d.address_country})" for d in method.destinations],
              )
            try:
              # Map ShippingDestination to PostalAddress for service call
              # Using strong types from SDK
              address_obj = PostalAddress(
                street_address=selected_dest.street_address,
                address_locality=selected_dest.address_locality,
                address_region=selected_dest.address_region,
                postal_code=selected_dest.postal_code,
                address_country=selected_dest.address_country,
              )

              # Get options from service
              # We calculate based on the items in this method/group
              # For simplicity, passing method's line_item_ids if available,
              # else all.
              all_li_ids = [li.id for li in checkout.line_items]
              target_li_ids = method.line_item_ids or all_li_ids

              # Map Line Item IDs to Product IDs for the service
              target_product_ids = []
              for li_uuid in target_li_ids:
                li = next(
                  (item for item in checkout.line_items if item.id == li_uuid),
                  None,
                )
                if li:
                  target_product_ids.append(li.item.id)

              calculated_options_resp = (
                await self.fulfillment_service.calculate_options(
                  self.transactions_session,
                  address_obj,
                  promotions=promotions,
                  subtotal=grand_total,
                  line_item_ids=target_product_ids,
                )
              )
              calculated_options = calculated_options_resp
            except (ValueError, TypeError) as e:
              logging.error("Failed to calculate options: %s", e)

        # 2. Generate or Update Groups
        if method.selected_destination_id and not method.groups:
          # Generate new group
          group = FulfillmentGroup(
            id=f"group_{uuid.uuid4()}",
            line_item_ids=method.line_item_ids,
            options=calculated_options,
          )
          method.groups = [group]
        elif method.groups:
          # Update existing groups with fresh options
          for group in method.groups:
            # Refresh options if they changed due to address/item update
            if calculated_options:
              group.options = calculated_options

            # Recalculate Totals based on Group Selection
            if group.selected_option_id and group.options:
              selected_opt = next(
                (o for o in group.options if o.id == group.selected_option_id),
                None,
              )
              if selected_opt:
                # Avoid double counting if already added.
                # Multiple groups can have costs.
                # We assume each group adds to the total.
                opt_total = next(
                  (t.amount for t in selected_opt.totals if t.type == "total"),
                  0,
                )
                grand_total += opt_total
                checkout.totals.append(
                  TotalResponse(type="fulfillment", amount=opt_total)
                )

    # Discount Logic
    if not checkout.discounts:
      checkout.discounts = DiscountsObject()

    # The server is the authority for applied discounts (discount.json marks
    # applied as ucp_request:"omit"). _recalculate_totals runs on every
    # create/update, and a reloaded checkout already carries the applied
    # entries from the previous response. Rebuild the list from scratch,
    # mirroring how `totals` is rebuilt above, so entries do not accumulate
    # on every recalculation (e.g. an update that omits the discounts field).
    checkout.discounts.applied = None

    if checkout.discounts.codes:
      # Batch fetch discounts to avoid N+1 queries
      discounts = await db.get_discounts_by_codes(
        self.transactions_session, checkout.discounts.codes
      )
      # Create a map for easy lookup by code (preserving request order if
      # needed)
      # Codes are matched case-insensitively by business (discount.md);
      # the applied entry echoes the canonical stored code.
      discount_map = {d.code.upper(): d for d in discounts}

      for code in checkout.discounts.codes:
        discount_obj = discount_map.get(code.upper())
        if discount_obj:
          discount_amount = 0
          if discount_obj.type == "percentage":
            discount_amount = int(grand_total * (discount_obj.value / 100))
          elif discount_obj.type == "fixed_amount":
            discount_amount = discount_obj.value

          if discount_amount > 0:
            grand_total -= discount_amount
            if checkout.discounts.applied is None:
              checkout.discounts.applied = []
            checkout.discounts.applied.append(
              AppliedDiscount(
                code=discount_obj.code,
                title=discount_obj.description,
                amount=discount_amount,
                allocations=[
                  Allocation(
                    path="$.totals[?(@.type=='subtotal')]",
                    amount=discount_amount,
                  )
                ],
              )
            )
            # totals[] carries the signed effect on the receipt: negative for
            # a discount (total.json exclusiveMaximum: 0). applied[].amount
            # above stays positive as the magnitude (discount.md).
            checkout.totals.append(
              TotalResponse(type="discount", amount=-discount_amount)
            )

    # Coins come off last: the total is what is left to pay by card.
    grand_total = await coin_service.apply(
      self.transactions_session, checkout, grand_total
    )

    checkout.totals.append(TotalResponse(type="total", amount=grand_total))

    # The slot of each deal booked at purchase, checked against the calendar.
    await booking_service.attach(
      self.products_session, self.transactions_session, checkout
    )

  async def _process_payment(self, payment: PaymentCreateRequest) -> str | None:
    """Check the payment instrument; return the token the rail will charge.

    The mock handler decides here, by its token, whether the payment goes
    through. The `stripe` handler only checks the token's shape (a payment
    method, `pm_…`): Stripe itself decides when the rail locks the amount.
    """
    instruments = payment.instruments
    if not instruments:
      raise InvalidRequestError("Missing payment instruments")

    # In 01-23 SDK, selected_instrument_id is removed.
    # We process the first provided instrument.
    selected_instrument = instruments[0]

    handler_id = getattr(selected_instrument, "handler_id", None)
    if not handler_id:
      raise InvalidRequestError("Missing handler_id in instrument")

    credential = getattr(selected_instrument, "credential", None)
    if not credential:
      raise InvalidRequestError("Missing credentials in instrument")

    # If it's a RootModel (like PaymentCredential), unwrap it to get the actual
    # credential data
    if hasattr(credential, "root"):
      credential = credential.root

    token = None

    if credential.type == "card":
      # Handle card details
      number = getattr(credential, "number", "unknown")
      logger.info(
        "Processing card payment for card ending in %s",
        number[-4:] if number else "unknown",
      )
      return None
    elif credential.type == "token":
      token = getattr(credential, "token", None)
    elif isinstance(credential, dict):
      type_val = credential.get("type")
      if type_val == "card":
        number = credential.get("number", "unknown")
        logger.info(
          "Processing card payment for card ending in %s",
          number[-4:] if number else "unknown",
        )
        return None
      elif type_val == "token":
        token = credential.get("token")
    else:
      # Fallback for unknown types if model validation allowed extras or
      # different types
      logger.warning("Unknown credential type: %s", type(credential))
      token = getattr(credential, "token", None)

    if handler_id == payment_rail.StripeRail.name:
      if not token or not token.startswith("pm_"):
        raise PaymentFailedError(
          "The stripe handler takes a PaymentMethod (pm_…) as the token",
          code="UNKNOWN_TOKEN",
        )
      return token
    if handler_id == "mock_payment_handler":
      if token == "success_token":
        return token  # Success
      elif token == "fail_token":
        raise PaymentFailedError(
          "Payment Failed: Insufficient Funds (Mock)",
          code="INSUFFICIENT_FUNDS",
        )
      elif token == "fraud_token":
        raise PaymentFailedError(
          "Payment Failed: Fraud Detected (Mock)",
          code="FRAUD_DETECTED",
          status_code=403,
        )
      else:
        raise PaymentFailedError(
          f"Unknown mock token: {token}", code="UNKNOWN_TOKEN"
        )
    elif handler_id == "google_pay":
      # Accept any token for now, or specific ones
      return token
    elif handler_id == "shop_pay":
      # For shop_pay, we expect a 'shop_token' credential type.
      # Since we don't have a real backend, we accept it if present.
      # The token value validation logic is similar to mock_payment_handler
      # for this test. Or just accept any token.
      return token
    else:
      # Unknown handler
      raise InvalidRequestError(f"Unsupported payment handler: {handler_id}")
