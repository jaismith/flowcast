"""The control table (DynamoDB `flowcast-control`): per-site state and per-run locks (production-architecture.md §4-5).

Items (partition key `pk`, sort key `sk`):

| pk            | sk                        | fields                                                                          |
|---------------|---------------------------|---------------------------------------------------------------------------------|
| SITE#{id}     | STATE                     | awake_until, last_visit, visit_days, alerts, last_issue, last_issue_time,        |
|               |                           | last_published, last_run_issue, last_run_status, last_run_trigger               |
| SITE#{id}     | RUN#{issue}#{flow model}  | status (running/done/failed), trigger, lease_until, attempts, started, finished |

A run starts only if its conditional put succeeds (no record, failed with < 3 attempts, or an expired lease), so a
wake and a scheduled cycle for the same issue collapse into one run. Times are ISO 8601 UTC strings.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

import boto3
from boto3.dynamodb.conditions import Attr, Key
from botocore.exceptions import ClientError

from . import config
from .issues import iso, issue_key, latest_issue, parse_iso, parse_issue, utcnow
from .registry import ServedSite


def site_pk(site_id: str) -> str:
    return f"SITE#{site_id}"


def run_sk(issue: str, model: str) -> str:
    return f"RUN#{issue}#{model}"


@dataclass(frozen=True)
class SiteStatus:
    id: str
    status: str  # active | snoozed | waking | delayed | paused
    always_on: bool
    always_on_reasons: list[str]
    awake_until: datetime | None
    last_issue: str | None
    run: dict | None


class Control:
    def __init__(self, table: str | None = None, resource=None):
        self.table = (resource or boto3.resource("dynamodb")).Table(table or config.Settings().table)

    # ------------------------------------------------------------------ reads

    def site(self, site_id: str) -> dict:
        return self.table.get_item(Key={"pk": site_pk(site_id), "sk": "STATE"}).get("Item") or {"pk": site_pk(site_id), "sk": "STATE"}

    def sites(self) -> dict[str, dict]:
        items, kwargs = [], {"FilterExpression": Attr("sk").eq("STATE")}
        while True:
            page = self.table.scan(**kwargs)
            items += page["Items"]
            if "LastEvaluatedKey" not in page:
                break
            kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]
        return {i["pk"].removeprefix("SITE#"): i for i in items}

    def latest_run(self, site_id: str) -> dict | None:
        page = self.table.query(KeyConditionExpression=Key("pk").eq(site_pk(site_id)) & Key("sk").begins_with("RUN#"), ScanIndexForward=False, Limit=1)
        return page["Items"][0] if page["Items"] else None

    def run(self, site_id: str, issue: str, model: str) -> dict | None:
        return self.table.get_item(Key={"pk": site_pk(site_id), "sk": run_sk(issue, model)}).get("Item")

    # ------------------------------------------------------------------ visits

    def record_visit(self, site: ServedSite, now: datetime | None = None, visit_active_count: int | None = None) -> tuple[dict, bool]:
        """Extend the site's active window for a page visit. Returns (site item, whether it was capped).

        7 days rolling after every visit; 30 days once the site was visited on >= 3 distinct days in 14. A site that
        isn't already visit-active only becomes active while fewer than `VISIT_ACTIVE_CAP` sites are.
        """
        now = now or utcnow()
        item = self.site(site.site_id)
        day = now.strftime("%Y-%m-%d")
        horizon = (now - timedelta(days=config.POPULAR_WINDOW_DAYS)).strftime("%Y-%m-%d")
        days = sorted({d for d in item.get("visit_days", []) if d > horizon} | {day})
        awake_until = parse_iso(item.get("awake_until"))
        already = awake_until is not None and awake_until > now
        if not site.pinned and not already:
            count = visit_active_count if visit_active_count is not None else self.visit_active_count(now)
            if count >= config.VISIT_ACTIVE_CAP:
                self.table.update_item(Key={"pk": site_pk(site.site_id), "sk": "STATE"}, UpdateExpression="SET last_visit = :v, visit_days = :d",
                                       ExpressionAttributeValues={":v": iso(now), ":d": days})
                return {**item, "last_visit": iso(now), "visit_days": days}, True
        window = config.POPULAR_DAYS if len(days) >= config.POPULAR_MIN_VISIT_DAYS else config.SNOOZE_DAYS
        until = max(filter(None, [awake_until, now + timedelta(days=window)]))
        self.table.update_item(
            Key={"pk": site_pk(site.site_id), "sk": "STATE"},
            UpdateExpression="SET last_visit = :v, visit_days = :d, awake_until = :a",
            ExpressionAttributeValues={":v": iso(now), ":d": days, ":a": iso(until)},
        )
        return {**item, "last_visit": iso(now), "visit_days": days, "awake_until": iso(until)}, False

    def visit_active_count(self, now: datetime | None = None, pinned: set[str] | None = None) -> int:
        now = now or utcnow()
        pinned = pinned or set()
        return sum(1 for site_id, i in self.sites().items() if site_id not in pinned and (parse_iso(i.get("awake_until")) or now) > now)

    def set_alerts(self, site_id: str, delta: int) -> int:
        """Alerts hook: the alerts service adds (+1) or removes (-1) a subscription; sites with any are always on."""
        out = self.table.update_item(
            Key={"pk": site_pk(site_id), "sk": "STATE"},
            UpdateExpression="SET alerts = if_not_exists(alerts, :z) + :d",
            ExpressionAttributeValues={":z": 0, ":d": delta},
            ReturnValues="UPDATED_NEW",
        )
        n = int(out["Attributes"]["alerts"])
        if n < 0:
            self.table.update_item(Key={"pk": site_pk(site_id), "sk": "STATE"}, UpdateExpression="SET alerts = :z", ExpressionAttributeValues={":z": 0})
            n = 0
        return n

    # ------------------------------------------------------------------ runs

    def acquire(self, site_id: str, issue: str, model: str, trigger: str, now: datetime | None = None) -> bool:
        """Take the run lock for (site, issue, model); False if another run holds it or it is done."""
        now = now or utcnow()
        try:
            self.table.update_item(
                Key={"pk": site_pk(site_id), "sk": run_sk(issue, model)},
                UpdateExpression="SET #s = :running, lease_until = :lease, started = :now, #t = :trigger, attempts = if_not_exists(attempts, :z) + :one",
                ConditionExpression="attribute_not_exists(pk) OR (#s = :failed AND attempts < :max) OR (#s = :running AND lease_until < :now)",
                ExpressionAttributeNames={"#s": "status", "#t": "trigger"},
                ExpressionAttributeValues={
                    ":running": "running", ":failed": "failed", ":lease": iso(now + timedelta(minutes=config.RUN_LEASE_MIN)),
                    ":now": iso(now), ":trigger": trigger, ":z": 0, ":one": 1, ":max": config.MAX_ATTEMPTS,
                },
            )
        except ClientError as err:
            if err.response["Error"]["Code"] == "ConditionalCheckFailedException":
                return False
            raise
        self.table.update_item(
            Key={"pk": site_pk(site_id), "sk": "STATE"},
            UpdateExpression="SET last_run_issue = :i, last_run_status = :s, last_run_trigger = :t",
            ExpressionAttributeValues={":i": issue, ":s": "running", ":t": trigger},
        )
        return True

    def finish(self, site_id: str, issue: str, model: str, ok: bool, now: datetime | None = None, error: str | None = None, seconds: float | None = None,
               has_temp: bool | None = None) -> None:
        now = now or utcnow()
        status = "done" if ok else "failed"
        values = {":s": status, ":f": iso(now), ":e": (error or "")[:500], ":sec": str(round(seconds or 0.0, 1))}
        self.table.update_item(
            Key={"pk": site_pk(site_id), "sk": run_sk(issue, model)},
            UpdateExpression="SET #s = :s, finished = :f, #e = :e, seconds = :sec",
            ExpressionAttributeNames={"#s": "status", "#e": "error"},
            ExpressionAttributeValues=values,
        )
        update = "SET last_run_issue = :i, last_run_status = :s"
        vals = {":i": issue, ":s": status}
        if ok:
            update += ", last_issue = :i, last_issue_time = :it, last_published = :f"
            vals |= {":it": iso(parse_issue(issue)), ":f": iso(now)}
            if has_temp is not None:
                update += ", has_temp = :t"
                vals[":t"] = has_temp
        self.table.update_item(Key={"pk": site_pk(site_id), "sk": "STATE"}, UpdateExpression=update, ExpressionAttributeValues=vals)


    def set_live_key(self, site_id: str, key: str) -> None:
        """What the published live.json says (status, awake_until, issue), so the light build republishes on change."""
        self.table.update_item(Key={"pk": site_pk(site_id), "sk": "STATE"}, UpdateExpression="SET live_key = :k", ExpressionAttributeValues={":k": key})


# ---------------------------------------------------------------------------------------------- state


def always_on(site: ServedSite, item: dict) -> list[str]:
    reasons = ["pinned"] if site.pinned else []
    alerts = int(item.get("alerts", 0) or 0)
    if alerts > 0:
        reasons.append(f"alerts:{alerts}")
    return reasons


def is_active(site: ServedSite, item: dict, now: datetime) -> bool:
    awake = parse_iso(item.get("awake_until"))
    return bool(always_on(site, item)) or (awake is not None and awake > now)


def status(site: ServedSite, item: dict, run: dict | None, now: datetime | None = None, capped: bool = False) -> SiteStatus:
    now = now or utcnow()
    reasons = always_on(site, item)
    awake = parse_iso(item.get("awake_until"))
    active = bool(reasons) or (awake is not None and awake > now)
    last_issue = item.get("last_issue")
    running = run is not None and run.get("status") == "running" and (parse_iso(run.get("lease_until")) or now) > now
    if running and run.get("trigger") == "wake":
        state = "waking"
    elif capped or (active and run is not None and run.get("status") == "failed" and run.get("trigger") == "wake" and (not last_issue or run["sk"].split("#")[1] > last_issue)):
        state = "paused"
    elif not active:
        state = "snoozed"
    elif last_issue and now - parse_issue(last_issue) > timedelta(hours=config.DELAYED_AFTER_H):
        state = "delayed"
    else:
        state = "active"
    return SiteStatus(site.site_id, state, bool(reasons), reasons, None if reasons else (awake if awake and awake > now else None), last_issue, run)


def target_issue(now: datetime | None = None) -> str:
    return issue_key(latest_issue(now))
