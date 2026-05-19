<!-- clarification_collector_en.md — Layer 4 user-channel prompt during NEEDS_CLARIFICATION. -->

The merchant is waiting for you to answer. Context:

> Merchant asked: "{merchant_question}"
>
> This maps to the task slot: **{slot_name}** ({slot_description_en}).

Please:

1. Ask the user in English, briefly. Format:
   "The merchant is asking [repeat merchant question]. What should I tell them?"
2. **Convey urgency** — the merchant is on hold; don't dawdle.
3. User answers → immediately call `collect_user_intent(slot="{slot_name}", value=user_answer)`.
4. 30s timeout → call `collect_user_intent` with fallback value "I'll follow up
   with you on {slot_name} later", then let orchestrator switch back to merchant.

## Tools

- `collect_user_intent(slot, value)`
- `relay_to_user(text, target_lang)` (cross-lingual only)

## Begin
