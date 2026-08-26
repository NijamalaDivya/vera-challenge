import os
import re
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

APP_VERSION = os.getenv("BOT_VERSION", "1.1.0")
app = FastAPI(title="Vera Challenge Bot", version=APP_VERSION)
STARTED = time.time()

# In-memory state is explicitly allowed by the challenge.
STORE: dict[str, dict[str, dict[str, Any]]] = {
    "category": {},
    "merchant": {},
    "customer": {},
    "trigger": {},
}
VERSIONS: dict[tuple[str, str], int] = {}
SUPPRESSED: set[str] = set()
CONVERSATIONS: dict[str, dict[str, Any]] = {}

VALID_SCOPES = set(STORE)


class ContextRequest(BaseModel):
    scope: str
    context_id: str
    version: int = Field(ge=1)
    payload: dict[str, Any]
    delivered_at: Optional[str] = None


class TickRequest(BaseModel):
    now: Optional[str] = None
    available_triggers: list[str] = Field(default_factory=list)


class ReplyRequest(BaseModel):
    conversation_id: str
    merchant_id: Optional[str] = None
    customer_id: Optional[str] = None
    from_role: str
    message: str
    received_at: Optional[str] = None
    turn_number: int = Field(default=1, ge=1)


@app.exception_handler(Exception)
async def generic_exception_handler(request: Request, exc: Exception):
    # Keep malformed/unexpected requests JSON-shaped and never expose internals.
    return JSONResponse(status_code=400, content={"accepted": False, "reason": "invalid_request"})


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def owner_name(merchant: dict) -> str:
    ident = merchant.get("identity", {}) or {}
    if ident.get("owner_first_name"):
        return str(ident["owner_first_name"])
    raw = str(ident.get("name", "there"))
    # Dr. Meera's Dental Clinic -> Dr. Meera; Studio11 -> Studio11
    return raw.split("'s", 1)[0].strip() or "there"


def category_for(merchant: dict) -> dict:
    return STORE["category"].get(str(merchant.get("category_slug", "")), {})


def get_merchant(mid: Optional[str]) -> Optional[dict]:
    return STORE["merchant"].get(mid) if mid else None


def get_customer(cid: Optional[str]) -> Optional[dict]:
    return STORE["customer"].get(cid) if cid else None


def active_offer(merchant: dict) -> Optional[dict]:
    for offer in merchant.get("offers", []) or []:
        if offer.get("status") == "active":
            return offer
    return None


def fmt_pct(value: Any) -> str:
    try:
        return f"{float(value) * 100:.1f}%"
    except Exception:
        return str(value)


def first_name(customer: dict) -> str:
    raw = str(customer.get("identity", {}).get("name") or "there").strip()
    parts = raw.split()
    if parts and parts[0].rstrip(".").lower() in {"mr", "mrs", "ms", "dr", "miss"}:
        parts = parts[1:]
    return parts[0] if parts else "there"


def digest_item(category: dict, trigger: dict) -> Optional[dict]:
    payload = trigger.get("payload", {}) or {}
    wanted = (
        payload.get("top_item_id")
        or payload.get("digest_item_id")
        or payload.get("top_item", {}).get("id")
    )
    items = category.get("digest", []) or []
    if wanted:
        for item in items:
            if item.get("id") == wanted:
                return item
    kind = trigger.get("kind", "")
    preferred = {
        "research_digest": "research",
        "category_research_digest_release": "research",
        "regulation_change": "compliance",
        "compliance": "compliance",
    }.get(kind)
    if preferred:
        for item in items:
            if item.get("kind") == preferred:
                return item
    return items[0] if items else None


def has_customer_consent(customer: dict, kind: str) -> bool:
    consent = customer.get("consent", {}) or {}
    scopes = set(consent.get("scope", []) or [])
    if not consent.get("opted_in_at") or not scopes:
        return False
    required = {
        "recall_due": {"recall_reminders"},
        "customer_lapsed_soft": {"recall_reminders", "winback_offers"},
        "customer_lapsed_hard": {"winback_offers", "program_updates"},
        "winback_eligible": {"winback_offers"},
        "appointment_tomorrow": {"appointment_reminders"},
        "trial_followup": {"kids_program_updates", "program_updates"},
        "wedding_package_followup": {"bridal_package_followup", "appointment_reminders"},
        "chronic_refill_due": {"refill_reminders"},
    }.get(kind)
    if required is None:
        return bool(scopes)
    return bool(scopes.intersection(required))


def localized_prefix(customer: dict) -> str:
    pref = str(customer.get("identity", {}).get("language_pref", "")).lower()
    if "hi" in pref:
        return "Hi"
    return "Hi"


def compose(category: dict, merchant: dict, trigger: dict, customer: Optional[dict] = None) -> dict:
    kind = str(trigger.get("kind", "unknown"))
    payload = trigger.get("payload", {}) or {}
    name = owner_name(merchant)
    category_slug = category.get("slug", merchant.get("category_slug", "business"))
    suppression = trigger.get("suppression_key") or trigger.get("id") or uuid.uuid4().hex

    # ---------------- Customer-facing ----------------
    if customer:
        cn = first_name(customer)
        if not has_customer_consent(customer, kind):
            return {
                "skip": True,
                "body": "",
                "cta": "none",
                "send_as": "merchant_on_behalf",
                "suppression_key": suppression,
                "rationale": "Customer context does not contain consent for this message type; no outbound message is sent.",
            }

        if kind in {"recall_due", "customer_lapsed_soft", "customer_lapsed_hard", "winback_eligible"}:
            service = str(payload.get("service_due", "next visit")).replace("_", " ")
            due = payload.get("due_date")
            slots = payload.get("available_slots") or []
            labels = [s.get("label") for s in slots[:2] if s.get("label")]
            if kind in {"customer_lapsed_hard", "winback_eligible"} and not payload.get("service_due"):
                days = payload.get("days_since_last_visit") or payload.get("days_since_expiry")
                focus = payload.get("previous_focus")
                body = f"{localized_prefix(customer)} {cn}, it's been {days} days since your last visit" if days is not None else f"{localized_prefix(customer)} {cn}, we'd love to welcome you back"
                if focus:
                    body += f" — your previous focus was {str(focus).replace('_', ' ')}"
                body += ". Reply YES if you'd like a next-step option or STOP to opt out."
            else:
                body = f"{localized_prefix(customer)} {cn}, your {service} is due"
                if due:
                    body += f" around {due}"
                if labels:
                    body += f". I have {' or '.join(labels)} available"
                body += ". Reply YES to book or STOP to opt out."
            return {"body": body, "cta": "binary_yes_no", "send_as": "merchant_on_behalf",
                    "suppression_key": suppression,
                    "rationale": "Consent-backed recall/winback message uses supplied service, date and slots with one binary CTA."}

        if kind == "appointment_tomorrow":
            body = f"{localized_prefix(customer)} {cn}, a quick reminder that your appointment is tomorrow"
            if payload.get("appointment_time"):
                body += f" at {payload['appointment_time']}"
            body += ". Reply YES to confirm or STOP to opt out."
            return {"body": body, "cta": "binary_yes_no", "send_as": "merchant_on_behalf",
                    "suppression_key": suppression,
                    "rationale": "Appointment reminder is based only on supplied booking details and consent."}

        if kind == "wedding_package_followup":
            wedding = payload.get("wedding_date")
            window = str(payload.get("next_step_window_open", "next step")).replace("_", " ")
            body = f"{localized_prefix(customer)} {cn}, following up on your bridal plan"
            if wedding:
                body += f" for the {wedding} wedding date"
            body += f". The next-step window is {window}. Reply YES if you'd like the follow-up arranged or STOP to opt out."
            return {"body": body, "cta": "binary_yes_no", "send_as": "merchant_on_behalf",
                    "suppression_key": suppression,
                    "rationale": "Consent-backed bridal follow-up uses the supplied wedding date and next-step window with one binary CTA."}

        if kind == "trial_followup":
            options = payload.get("next_session_options") or []
            option = options[0].get("label") if options and isinstance(options[0], dict) else None
            body = f"{localized_prefix(customer)} {cn}, following up on your trial session"
            if payload.get("trial_date"):
                body += f" from {payload['trial_date']}"
            if option:
                body += f". The next available option is {option}"
            body += ". Reply YES if you'd like to continue or STOP to opt out."
            return {"body": body, "cta": "binary_yes_no", "send_as": "merchant_on_behalf",
                    "suppression_key": suppression,
                    "rationale": "Trial follow-up uses the supplied trial date and next-session option with a single CTA."}

        if kind == "chronic_refill_due":
            molecules = ", ".join(payload.get("molecule_list", [])[:3])
            body = f"{localized_prefix(customer)} {cn}, your refill reminder is coming up"
            if molecules:
                body += f" for {molecules}"
            if payload.get("stock_runs_out_iso"):
                body += f" before {payload['stock_runs_out_iso']}"
            body += ". Reply YES if you'd like the refill arranged or STOP to opt out."
            return {"body": body, "cta": "binary_yes_no", "send_as": "merchant_on_behalf",
                    "suppression_key": suppression,
                    "rationale": "Consent-backed refill reminder uses only the supplied medicine names and timing."}

        return {"body": f"{localized_prefix(customer)} {cn}, I have an update from the clinic. Reply YES for details or STOP to opt out.",
                "cta": "binary_yes_no", "send_as": "merchant_on_behalf", "suppression_key": suppression,
                "rationale": "Customer message is consent-backed and keeps a single low-friction CTA."}

    # ---------------- Merchant-facing ----------------
    perf = merchant.get("performance", {}) or {}
    signals = merchant.get("signals", []) or []
    peer = category.get("peer_stats", {}) or {}
    offer = active_offer(merchant)

    if kind in {"research_digest", "category_research_digest_release"}:
        item = digest_item(category, trigger)
        if item:
            title = item.get("title", "a new research item")
            source = item.get("source", "")
            trial = item.get("trial_n")
            detail = f"{trial}-patient " if trial else ""
            body = f"{name}, {source or 'a new category research update'} has a relevant item: {detail}{title}."
            if item.get("patient_segment"):
                body += f" It matches your {str(item['patient_segment']).replace('_', ' ')} cohort."
            body += " Want me to pull the key points and draft a patient-facing WhatsApp?"
        else:
            body = f"{name}, a new research update is available for {category_slug}. Want me to pull the relevant item and summarize it?"
        return {"body": body, "cta": "open_ended", "send_as": "vera", "suppression_key": suppression,
                "rationale": "Research trigger is anchored to the supplied digest item and source, then offers a concrete low-effort next step."}

    if kind in {"regulation_change", "compliance"}:
        item = digest_item(category, trigger)
        body = f"{name}, a compliance update may affect your {category_slug} practice."
        if item:
            body += f" {item.get('title', '')}"
            if item.get("source"):
                body += f" — {item['source']}."
        if payload.get("deadline_iso"):
            body += f" Effective/deadline: {payload['deadline_iso']}."
        body += " Want me to summarize the change and the practical checklist?"
        return {"body": body, "cta": "open_ended", "send_as": "vera", "suppression_key": suppression,
                "rationale": "Compliance message uses the supplied rule, source and deadline without inventing regulatory claims."}

    if kind in {"perf_dip", "seasonal_perf_dip"}:
        metric = payload.get("metric") or "performance"
        delta = payload.get("delta_pct")
        if delta is None:
            delta = (perf.get("delta_7d") or {}).get(f"{metric}_pct")
        body = f"{name}, your {metric} moved"
        if delta is not None:
            body += f" {fmt_pct(delta)} in the {payload.get('window', '7d')} window"
        elif perf.get("views") is not None and perf.get("calls") is not None:
            body += f" to {perf['calls']} calls from {perf['views']} views"
        if kind == "seasonal_perf_dip" and payload.get("season_note"):
            body += f". The supplied seasonal note is {payload['season_note'].replace('_', ' ')}"
        body += ". Want me to pinpoint one listing-side fix?"
        return {"body": body, "cta": "open_ended", "send_as": "vera", "suppression_key": suppression,
                "rationale": "Performance message cites the trigger's current metric and asks for one concrete diagnostic next step."}

    if kind == "perf_spike":
        metric = payload.get("metric", "views")
        delta = payload.get("delta_pct")
        if delta is None:
            delta = (perf.get("delta_7d") or {}).get(f"{metric}_pct")
        body = f"{name}, your {metric} is up"
        if delta is not None:
            body += f" {fmt_pct(delta)} in the {payload.get('window', '7d')} window"
        if payload.get("likely_driver"):
            body += f", with {str(payload['likely_driver']).replace('_', ' ')} as a likely driver"
        body += ". Want me to show how to build on it?"
        return {"body": body, "cta": "open_ended", "send_as": "vera", "suppression_key": suppression,
                "rationale": "Positive performance trigger is tied to the supplied metric change and likely driver."}

    if kind == "renewal_due":
        days = payload.get("days_remaining")
        plan = payload.get("plan") or merchant.get("subscription", {}).get("plan")
        amount = payload.get("renewal_amount")
        body = f"{name}, your {plan or 'subscription'} renewal is {days} days away" if days is not None else f"{name}, your subscription renewal is coming up"
        if amount is not None:
            body += f" at the supplied renewal amount of ₹{amount}"
        body += ". Want me to help you review what to keep before renewal?"
        return {"body": body, "cta": "open_ended", "send_as": "vera", "suppression_key": suppression,
                "rationale": "Renewal message uses the exact supplied plan, timing and amount and avoids urgency beyond the trigger."}

    if kind == "festival_upcoming":
        event = payload.get("festival", "festival")
        date = payload.get("date")
        body = f"{name}, {event} is coming up"
        if date:
            body += f" on {date}"
        if offer:
            body += f". Your active offer is {offer.get('title')}"
        body += ". Want me to draft one timely customer post?"
        return {"body": body, "cta": "open_ended", "send_as": "vera", "suppression_key": suppression,
                "rationale": "Seasonal message uses the supplied event date and existing active offer."}

    if kind == "wedding_package_followup":
        wedding = payload.get("wedding_date")
        window = payload.get("next_step_window_open", "the next step")
        body = f"{name}, following up on the bridal lead"
        if wedding:
            body += f" for the {wedding} wedding date"
        body += f". The supplied next-step window is {str(window).replace('_', ' ')}. Want me to draft the follow-up?"
        return {"body": body, "cta": "open_ended", "send_as": "vera", "suppression_key": suppression,
                "rationale": "Follow-up is tied to the supplied wedding date and next-step window."}

    if kind in {"curious_ask_due"}:
        ask = payload.get("ask_template")
        body = f"{name}, quick question for your {category_slug} profile"
        if ask:
            body += f": {str(ask).replace('_', ' ')}"
        body += ". What are customers asking for most this week?"
        return {"body": body, "cta": "open_ended", "send_as": "vera", "suppression_key": suppression,
                "rationale": "Curiosity trigger asks the merchant a simple category-relevant question rather than sending a generic pitch."}

    if kind == "winback_eligible":
        days = payload.get("days_since_expiry")
        lapsed = payload.get("lapsed_customers_added_since_expiry")
        body = f"{name}, your profile has been inactive for {days} days" if days is not None else f"{name}, your profile has a win-back opportunity"
        if lapsed is not None:
            body += f", with {lapsed} lapsed customers added since expiry"
        body += ". Want me to sketch one low-effort win-back message?"
        return {"body": body, "cta": "open_ended", "send_as": "vera", "suppression_key": suppression,
                "rationale": "Win-back message uses supplied inactivity and customer-count signals and offers one concrete action."}

    if kind == "ipl_match_today":
        match = payload.get("match", "today's match")
        venue = payload.get("venue")
        time_text = payload.get("match_time_iso")
        body = f"{name}, {match} is on today"
        if venue:
            body += f" at {venue}"
        if time_text:
            body += f" ({time_text})"
        if offer:
            body += f". Your active offer is {offer.get('title')}"
        body += ". Want me to draft a match-day post?"
        return {"body": body, "cta": "open_ended", "send_as": "vera", "suppression_key": suppression,
                "rationale": "Event message is anchored to the supplied match, venue/time and existing offer."}

    if kind == "review_theme_emerged":
        theme = payload.get("theme", "a review theme")
        count = payload.get("occurrences_30d")
        trend = payload.get("trend")
        body = f"{name}, {theme.replace('_', ' ')} is showing in recent reviews"
        if count is not None:
            body += f" ({count} occurrences in 30 days)"
        if trend:
            body += f", with the trend marked {trend}"
        body += ". Want me to turn this into one practical fix?"
        return {"body": body, "cta": "open_ended", "send_as": "vera", "suppression_key": suppression,
                "rationale": "Review message uses the supplied theme, count and trend rather than making a new claim."}

    if kind == "milestone_reached":
        metric = payload.get("metric", "milestone")
        value = payload.get("value_now")
        milestone = payload.get("milestone_value") or payload.get("value")
        body = f"{name}, you are close to the {milestone} {metric.replace('_', ' ')} milestone" if milestone else f"{name}, you just reached a {metric.replace('_', ' ')} milestone"
        if value is not None:
            body += f" at {value}"
        body += ". Want me to turn the milestone into a customer-facing post?"
        return {"body": body, "cta": "open_ended", "send_as": "vera", "suppression_key": suppression,
                "rationale": "Milestone message uses exact supplied milestone values and offers a concrete follow-on."}

    if kind == "active_planning_intent":
        topic = str(payload.get("intent_topic", "your planned update")).replace("_", " ")
        last = payload.get("merchant_last_message")
        body = f"{name}, for your {topic} idea"
        if last:
            body += f", you asked what it could look like: {last}"
        body += ". Want me to turn that into a simple first draft?"
        return {"body": body, "cta": "open_ended", "send_as": "vera", "suppression_key": suppression,
                "rationale": "Planning-intent trigger continues the merchant's stated idea instead of re-qualifying it."}

    if kind == "trial_followup":
        # Merchant-side fallback if no customer context was attached.
        trial = payload.get("trial_date")
        body = f"{name}, a trial follow-up is due"
        if trial:
            body += f" from the session on {trial}"
        body += ". Want me to draft the next-step message?"
        return {"body": body, "cta": "open_ended", "send_as": "vera", "suppression_key": suppression,
                "rationale": "Trial follow-up references the supplied trial date and keeps the action low effort."}

    if kind == "supply_alert":
        molecule = payload.get("molecule", "the affected medicine")
        batches = ", ".join(payload.get("affected_batches", [])[:3])
        body = f"{name}, there is a supplied stock alert for {molecule}"
        if batches:
            body += f" affecting batches {batches}"
        body += ". Want me to summarize the alert details you already have?"
        return {"body": body, "cta": "open_ended", "send_as": "vera", "suppression_key": suppression,
                "rationale": "Supply alert uses only the supplied molecule and affected batches and avoids unsupported medical advice."}

    if kind == "category_seasonal":
        trends = payload.get("trends") or []
        trend_text = ", ".join(str(x).replace("_", " ") for x in trends[:3])
        body = f"{name}, the supplied {payload.get('season', 'seasonal')} trend points to {trend_text or 'a demand shift'}"
        body += ". Want me to turn the strongest signal into one shelf/profile action?"
        return {"body": body, "cta": "open_ended", "send_as": "vera", "suppression_key": suppression,
                "rationale": "Seasonal trigger cites supplied demand signals and proposes one operational next step."}

    if kind == "gbp_unverified":
        verified = payload.get("verified")
        path = payload.get("verification_path")
        uplift = payload.get("estimated_uplift_pct")
        body = f"{name}, your listing is currently marked unverified"
        if verified is False and path:
            body += f"; the supplied verification path is {path}"
        if uplift is not None:
            body += f" and the supplied estimate is {fmt_pct(uplift)}"
        body += ". Want me to walk through the verification step?"
        return {"body": body, "cta": "open_ended", "send_as": "vera", "suppression_key": suppression,
                "rationale": "Listing-verification message uses supplied status/path and clearly labels the supplied uplift estimate."}

    if kind == "cde_opportunity":
        credits = payload.get("credits")
        fee = payload.get("fee")
        body = f"{name}, there is a category education opportunity in the supplied digest"
        if credits is not None:
            body += f" with {credits} credits"
        if fee:
            body += f" ({str(fee).replace('_', ' ')})"
        body += ". Want me to summarize the session details?"
        return {"body": body, "cta": "open_ended", "send_as": "vera", "suppression_key": suppression,
                "rationale": "Education opportunity uses only the supplied credits and fee information."}

    if kind == "competitor_opened":
        competitor = payload.get("competitor_name")
        distance = payload.get("distance_km")
        their_offer = payload.get("their_offer")
        body = f"{name}, {competitor or 'a nearby competitor'} opened"
        if distance is not None:
            body += f" {distance} km away"
        if their_offer:
            body += f" with {their_offer}"
        body += ". Want me to show one response option using your current offer?"
        return {"body": body, "cta": "open_ended", "send_as": "vera", "suppression_key": suppression,
                "rationale": "Competitor trigger cites the supplied competitor facts and turns them into one concrete response option."}

    if kind == "dormant_with_vera":
        days = payload.get("days_since_last_merchant_message")
        topic = payload.get("last_topic")
        body = f"{name}, it has been {days} days since the last merchant update" if days is not None else f"{name}, it has been a while since the last update"
        if topic:
            body += f" on {str(topic).replace('_', ' ')}"
        body += ". Want me to pick up with one useful next step?"
        return {"body": body, "cta": "open_ended", "send_as": "vera", "suppression_key": suppression,
                "rationale": "Dormancy trigger uses the supplied cadence and prior topic without spamming."}

    # Safe fallback.
    body = f"{name}, I have a {kind.replace('_', ' ')} update for your {category_slug} profile. Want me to show the most relevant next step?"
    return {"body": body, "cta": "open_ended", "send_as": "vera", "suppression_key": suppression,
            "rationale": "Fallback uses only the trigger kind and current merchant/category context, with one clear CTA."}


def validate_message(msg: dict, category: dict) -> dict:
    if msg.get("skip"):
        return msg
    body = re.sub(r"\s+", " ", str(msg.get("body", ""))).strip()
    if not body:
        raise ValueError("empty body")

    taboos = (category.get("voice", {}) or {}).get("taboos", []) or []
    # Avoid silently deleting claims; deterministic templates should not contain taboos.
    for taboo in taboos:
        if taboo and re.search(rf"\b{re.escape(str(taboo))}\b", body, re.I):
            raise ValueError("category taboo detected")

    cta = msg.get("cta")
    if cta not in {"binary_yes_no", "open_ended", "none"}:
        raise ValueError("invalid cta")
    if msg.get("send_as") not in {"vera", "merchant_on_behalf"}:
        raise ValueError("invalid send_as")
    msg["body"] = body
    return msg


@app.post("/v1/context")
def context(req: ContextRequest):
    if req.scope not in VALID_SCOPES:
        return JSONResponse(status_code=400, content={
            "accepted": False,
            "reason": "invalid_scope",
            "details": f"scope must be one of {sorted(VALID_SCOPES)}",
        })

    key = (req.scope, req.context_id)
    current = VERSIONS.get(key)
    if current is not None and req.version == current:
        return {
            "accepted": True,
            "ack_id": f"ack_{req.context_id}_v{req.version}",
            "stored_at": now_iso(),
        }
    if current is not None and req.version < current:
        return JSONResponse(status_code=409, content={
            "accepted": False,
            "reason": "stale_version",
            "current_version": current,
        })

    # Single-threaded request handling under normal Uvicorn execution makes this atomic
    # for the challenge's in-memory deployment model.
    STORE[req.scope][req.context_id] = req.payload
    VERSIONS[key] = req.version
    return {
        "accepted": True,
        "ack_id": f"ack_{req.context_id}_v{req.version}",
        "stored_at": now_iso(),
    }


@app.get("/v1/healthz")
def healthz():
    return {
        "status": "ok",
        "uptime_seconds": round(time.time() - STARTED, 3),
        "contexts_loaded": {scope: len(values) for scope, values in STORE.items()},
    }


@app.get("/v1/metadata")
def metadata():
    return {
        "team_name": os.getenv("TEAM_NAME", "Divya"),
        "team_members": [x.strip() for x in os.getenv("TEAM_MEMBERS", "Divya").split(",") if x.strip()],
        "model": os.getenv("BOT_MODEL", "deterministic-router-v1"),
        "approach": "FastAPI stateful context store + trigger-aware deterministic composer + multi-turn safety handler",
        "contact_email": os.getenv("CONTACT_EMAIL", ""),
        "version": APP_VERSION,
        "submitted_at": os.getenv("SUBMITTED_AT", now_iso()),
    }


@app.post("/v1/tick")
def tick(req: TickRequest):
    actions = []
    seen_pairs = set()

    for tid in req.available_triggers[:20]:
        trig = STORE["trigger"].get(tid)
        if not trig:
            continue

        payload = trig.get("payload", {}) or {}
        mid = trig.get("merchant_id") or payload.get("merchant_id")
        cid = trig.get("customer_id") or payload.get("customer_id")
        merchant = get_merchant(mid)
        if not merchant:
            continue

        pair = (mid, tid)
        if pair in seen_pairs:
            continue
        seen_pairs.add(pair)

        suppression = trig.get("suppression_key") or tid
        # Dedup per trigger/suppression key, not across unrelated merchants.
        dedup_key = f"{mid}:{suppression}"
        if dedup_key in SUPPRESSED:
            continue

        category = category_for(merchant)
        customer = get_customer(cid)
        if cid and not customer:
            continue

        try:
            msg = validate_message(compose(category, merchant, trig, customer), category)
        except Exception:
            continue
        if msg.get("skip"):
            SUPPRESSED.add(dedup_key)
            continue

        conv = f"conv_{mid}_{tid}_{uuid.uuid4().hex[:8]}"
        CONVERSATIONS[conv] = {
            "merchant_id": mid,
            "customer_id": cid,
            "trigger_id": tid,
            "suppression_key": suppression,
            "sent": [msg["body"]],
            "received": [],
            "ended": False,
        }

        template_name = "vera_customer_action_v1" if customer else "vera_contextual_v1"
        template_params = [owner_name(merchant), category.get("slug", "business"), kind_label(trig)]
        actions.append({
            "conversation_id": conv,
            "merchant_id": mid,
            "customer_id": cid,
            "send_as": msg["send_as"],
            "trigger_id": tid,
            "template_name": template_name,
            "template_params": template_params,
            "body": msg["body"],
            "cta": msg["cta"],
            "suppression_key": msg["suppression_key"],
            "rationale": msg["rationale"],
        })
        SUPPRESSED.add(dedup_key)

        if len(actions) >= 20:
            break

    return {"actions": actions}


def kind_label(trigger: dict) -> str:
    return str(trigger.get("kind", "update")).replace("_", " ")


def get_state_for_reply(req: ReplyRequest) -> dict:
    state = CONVERSATIONS.get(req.conversation_id)
    if state is None:
        state = {
            "merchant_id": req.merchant_id,
            "customer_id": req.customer_id,
            "sent": [],
            "received": [],
            "ended": False,
        }
        CONVERSATIONS[req.conversation_id] = state
    else:
        if req.merchant_id and not state.get("merchant_id"):
            state["merchant_id"] = req.merchant_id
        if req.customer_id and not state.get("customer_id"):
            state["customer_id"] = req.customer_id
    return state


@app.post("/v1/reply")
def reply(req: ReplyRequest):
    state = get_state_for_reply(req)
    text = (req.message or "").strip()
    low = text.lower()

    if state.get("ended"):
        return {"action": "end", "rationale": "Conversation was already closed; no further message is sent."}

    received = state.setdefault("received", [])
    received.append(text)

    # Hard opt-out takes absolute priority.
    if re.search(r"\b(stop|unsubscribe|do not message|don't message|not interested|no thanks|no thank you)\b", low):
        state["ended"] = True
        suppression = state.get("suppression_key")
        if suppression and state.get("merchant_id"):
            SUPPRESSED.add(f"{state['merchant_id']}:{suppression}")
        return {"action": "end", "rationale": "The recipient explicitly opted out or declined; conversation is closed and future sends are suppressed."}

    # Detect canned automation. Wait twice; after the third identical automated reply, exit.
    canned = any(phrase in low for phrase in [
        "thank you for contacting",
        "respond shortly",
        "our team will respond",
        "currently unavailable",
        "automated response",
        "out of office",
        "hamari team",
        "jald hi jawab",
    ])
    same_count = sum(1 for x in received if x == text)
    if canned or same_count >= 3:
        if same_count >= 3:
            state["ended"] = True
            return {"action": "end", "rationale": "Repeated automated/canned replies were detected; exiting instead of consuming more turns."}
        return {"action": "wait", "wait_seconds": 14400,
                "rationale": "Likely automated reply detected; backing off to avoid wasting turns and waiting for the owner."}

    # Positive intent should advance immediately, without another qualification question.
    positive = re.search(r"\b(yes|yep|yeah|sure|go ahead|do it|let'?s do it|please send|send it|okay|ok|proceed|confirm)\b", low)
    if positive:
        body = "Got it — moving ahead with the requested next step. I’ll keep it focused on the details already in this conversation."
        state.setdefault("sent", []).append(body)
        return {"action": "send", "body": body, "cta": "open_ended",
                "rationale": "Explicit positive intent detected; the bot advances to action instead of re-qualifying the merchant."}

    # Out-of-scope questions are acknowledged and redirected without hallucinating.
    if re.search(r"\b(gst|tax filing|legal case|loan|personal finance)\b", low):
        body = "That’s outside what I can help with directly. Coming back to the original update, I can help with the next step using the context already available."
        state.setdefault("sent", []).append(body)
        return {"action": "send", "body": body, "cta": "open_ended",
                "rationale": "Out-of-scope request is declined politely and redirected to the original business task."}

    # Respect explicit requests for time.
    if re.search(r"\b(later|tomorrow|busy|not now|give me time|call later)\b", low):
        return {"action": "wait", "wait_seconds": 1800,
                "rationale": "Recipient asked for time; backing off for 30 minutes rather than pushing another message."}

    # Simple question handling: don't invent an answer when the context is insufficient.
    if "?" in text:
        body = "Yes — I can help with that using the information already provided. If you want me to proceed, just say YES."
        state.setdefault("sent", []).append(body)
        return {"action": "send", "body": body, "cta": "binary_yes_no",
                "rationale": "Question is acknowledged without inventing facts, while preserving one low-friction next step."}

    return {"action": "wait", "wait_seconds": 3600,
            "rationale": "No clear engagement signal; waiting rather than sending a low-value follow-up."}


@app.post("/v1/teardown")
def teardown():
    STORE.update({"category": {}, "merchant": {}, "customer": {}, "trigger": {}})
    VERSIONS.clear()
    SUPPRESSED.clear()
    CONVERSATIONS.clear()
    return {"ok": True}
