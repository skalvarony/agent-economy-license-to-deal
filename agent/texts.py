"""What the agent says, in the person's language.

The scripted brain and the session use these; a model writes in the
language the memory asks for by itself.
"""

TEXTS: dict[str, dict[str, str]] = {
  "help": {
    "en": "Tell me what you'd like to do and any limits, for example: a spa day"
    " for two, refundable, under $120. I'll compare the shops and ask you"
    " before I pay.",
    "es": "Dime qué te apetece y tus límites, por ejemplo: un día de spa para"
    " dos, reembolsable, por menos de 120 $. Comparo las tiendas y te"
    " pregunto antes de pagar.",
  },
  "nothing_like_it": {
    "en": "I found nothing like that in the shops. ",
    "es": "No he encontrado nada parecido en las tiendas. ",
  },
  "nothing_fits": {
    "en": "I looked in {shops} and found nothing that fits. {whys}. Tell me"
    " what you'd change and I'll look again.",
    "es": "He mirado en {shops} y no hay nada que encaje. {whys}. Dime qué"
    " cambiarías y vuelvo a buscar.",
  },
  "rule": {
    "en": "{message} Change the rule on your page if you want it anyway.",
    "es": "{message} Cambia la regla en tu página si lo quieres igualmente.",
  },
  "shop_refused": {
    "en": "I found a deal that fits, but the shop wouldn't open a checkout for"
    " it: {message} Nothing was paid.",
    "es": "Hay una oferta que encaja, pero la tienda no ha abierto el"
    " checkout: {message} No se ha pagado nada.",
  },
  "proposal": {
    "en": "I'd buy {title} at {shop}. {paying}. I won't pay until you approve.",
    "es": "Compraría {title} en {shop}. {paying}. No pago hasta que apruebes.",
  },
  "proposal_booked": {
    "en": "I'd buy {title} at {shop}, booked for {when}. {paying}. I won't"
    " pay until you approve.",
    "es": "Compraría {title} en {shop}, reservado para el {when}. {paying}."
    " No pago hasta que apruebes.",
  },
  "ask_when": {
    "en": "{title} at {shop} fits, and it's booked for a date and time. When"
    " would you like to go? Next openings: {openings}. Tell me a day and an"
    " hour, for example: Saturday at 11:00.",
    "es": "{title} en {shop} encaja, y se reserva con día y hora. ¿Cuándo"
    " quieres ir? Próximos huecos: {openings}. Dime un día y una hora, por"
    " ejemplo: el sábado a las 11:00.",
  },
  "no_such_slot": {
    "en": "{title} has no free slot at that time on {day}. That day it has:"
    " {openings}. Pick one of those, or another day.",
    "es": "{title} no tiene hueco libre a esa hora el {day}. Ese día tiene:"
    " {openings}. Elige uno de esos, u otro día.",
  },
  "no_openings": {
    "en": "{title} at {shop} fits, but it has no free slot in the next days."
    " Tell me another day to try.",
    "es": "{title} en {shop} encaja, pero no tiene huecos libres en los"
    " próximos días. Dime otro día y lo intento.",
  },
  "slot_gone": {
    "en": "{shop} says that time has just filled up: {message} Nothing was"
    " paid. Tell me another time and I'll propose again.",
    "es": "{shop} dice que esa hora se acaba de llenar: {message} No se ha"
    " pagado nada. Dime otra hora y vuelvo a proponer.",
  },
  "which_purchase": {
    "en": "Which purchase do you mean? You have: {titles}.",
    "es": "¿Qué compra dices? Tienes: {titles}.",
  },
  "no_live_purchase": {
    "en": "I found no purchase of yours that is still open to change.",
    "es": "No encuentro ninguna compra tuya que aún se pueda cambiar.",
  },
  "moved": {
    "en": "Done. {title} at {shop} is now booked for {when}.",
    "es": "Hecho. {title} en {shop} queda reservado para el {when}.",
  },
  "cancelled_purchase": {
    "en": "Done. I cancelled {title} at {shop}: {back} goes back to your"
    " card{coins}.",
    "es": "Hecho. He cancelado {title} en {shop}: {back} vuelve a tu"
    " tarjeta{coins}.",
  },
  "move_proposed": {
    "en": "I can move {title} at {shop} to {when}. Approve it and I'll do"
    " it; nothing changes until you do.",
    "es": "Puedo mover {title} en {shop} al {when}. Apruébalo y lo hago; no"
    " cambia nada hasta que apruebes.",
  },
  "cancel_proposed": {
    "en": "I can cancel {title} at {shop}: {back} would go back to your"
    " card{coins}. Approve it and I'll do it; nothing is cancelled until"
    " you do.",
    "es": "Puedo cancelar {title} en {shop}: {back} volvería a tu"
    " tarjeta{coins}. Apruébalo y lo hago; no se cancela nada hasta que"
    " apruebes.",
  },
  "cannot_change": {
    "en": "{shop} wouldn't do it: {message} Nothing changed.",
    "es": "{shop} no lo ha permitido: {message} No ha cambiado nada.",
  },
  "card_pays": {
    "en": "Your card pays {total}",
    "es": "Tu tarjeta paga {total}",
  },
  "coins_cover": {
    "en": "Your coins cover it, so your card pays nothing",
    "es": "Tus coins lo cubren, así que la tarjeta no paga nada",
  },
  "not_open": {
    "en": "That proposal is no longer open, so I didn't buy anything.",
    "es": "Esa propuesta ya no está abierta, así que no he comprado nada.",
  },
  "declined": {
    "en": "I won't buy it. Tell me what you'd change and I'll look again.",
    "es": "No lo compro. Dime qué cambiarías y vuelvo a buscar.",
  },
  "refused": {
    "en": "{shop} refused the purchase: {message} Nothing was paid.",
    "es": "{shop} ha rechazado la compra: {message} No se ha pagado nada.",
  },
  "signature_refused": {
    "en": "{shop} wouldn't accept my signature ({code}): {message} Nothing"
    " was paid. Ask me again and I'll send a freshly signed request.",
    "es": "{shop} no ha aceptado mi firma ({code}): {message} No se ha"
    " pagado nada. Pídemelo otra vez y envío una petición firmada de nuevo.",
  },
  "bought": {
    "en": "Done. I bought {title} at {shop} and your voucher is ready.",
    "es": "Hecho. He comprado {title} en {shop} y tu voucher está listo.",
  },
  "changed": {
    "en": "{shop} changed the total from {old} to {new} after you approved."
    " You didn't approve that, so I didn't pay. Do you still want it?",
    "es": "{shop} ha cambiado el total de {old} a {new} después de tu"
    " aprobación. Eso no lo aprobaste, así que no he pagado. ¿Lo quieres"
    " igualmente?",
  },
  "switched": {
    "en": "Switched to {title} at {shop}. Your card pays {total}. Approve when"
    " you're ready.",
    "es": "Cambiado a {title} en {shop}. Tu tarjeta paga {total}. Aprueba"
    " cuando quieras.",
  },
  "unknown_option": {
    "en": "I don't know that option; ask me again.",
    "es": "No conozco esa opción; pídemelo otra vez.",
  },
  "failed": {
    "en": "Something went wrong on my side and I stopped. Nothing new was"
    " paid.",
    "es": "Algo ha fallado de mi lado y he parado. No se ha pagado nada nuevo.",
  },
  "standin": {
    "en": "The model didn't answer ({error}), so the scripted stand-in takes"
    " this turn.",
    "es": "El modelo no ha respondido ({error}), así que el guion se encarga"
    " de este turno.",
  },
  "cannot": {
    "en": "I can't go on: {error}. Nothing was paid.",
    "es": "No puedo seguir: {error}. No se ha pagado nada.",
  },
  "too_many_steps": {
    "en": "I took too many steps without getting anywhere, so I stopped.",
    "es": "He dado demasiados pasos sin llegar a nada, así que he parado.",
  },
}


def say(key: str, language: str = "en", **values: object) -> str:
  """Return a line in the person's language, filled in."""
  choices = TEXTS[key]
  return choices.get(language, choices["en"]).format(**values)
