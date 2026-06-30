#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = ["psycopg[binary]", "prometheus_client"]
# ///
"""Prometheus exporter for Mailman 3, reading directly from PostgreSQL.

This exporter reads from the database for speed instead of the REST API.
It uses hardcoded enums from the mailman source.
"""

import argparse
import json
import logging
import os
import signal
import sys
import threading
import time
from http.server import HTTPServer

import psycopg
from prometheus_client.core import REGISTRY, GaugeMetricFamily
from prometheus_client.exposition import MetricsHandler, generate_latest
from psycopg.conninfo import make_conninfo

log = logging.getLogger("mailman-exporter")

# These integer values are how Mailman stores enums in PostgreSQL.
# They're defined as IntEnums in the Mailman source and won't change
# without a database migration, If they do, this is where to look.
#
# MemberRole: src/mailman/interfaces/member.py:63 @ 7ed0824
MEMBER_ROLE_MEMBER = 1
MEMBER_ROLE_OWNER = 2
MEMBER_ROLE_MODERATOR = 3
MEMBER_ROLE_NONMEMBER = 4

ROLE_NAMES = {
    MEMBER_ROLE_MEMBER: "member",
    MEMBER_ROLE_OWNER: "owner",
    MEMBER_ROLE_MODERATOR: "moderator",
    MEMBER_ROLE_NONMEMBER: "nonmember",
}

# RequestType: src/mailman/interfaces/requests.py:30 @ 7ed0824
REQUEST_TYPE_HELD_MESSAGE = 1
REQUEST_TYPE_SUBSCRIPTION = 2
REQUEST_TYPE_UNSUBSCRIPTION = 3

REQUEST_TYPE_NAMES = {
    REQUEST_TYPE_HELD_MESSAGE: "held_message",
    REQUEST_TYPE_SUBSCRIPTION: "subscription",
    REQUEST_TYPE_UNSUBSCRIPTION: "unsubscription",
}


def _parse_filtered_member_counts(raw: str | None) -> dict[str, list[str]]:
    """Parse MAILMAN_FILTERED_MEMBER_COUNTS into {filter_name: [excluded list_ids]}.

    Each named group emits mailman_filtered_members_total{filter="<name>"}, the
    count of distinct verified member addresses that belong to at least one list
    *other* than the excluded ones. An address only on excluded lists is dropped.

    Accepts JSON, e.g.:
        {"subscribers": ["allowlist.example.com", "surveys.example.com"]}
    """
    if not raw or not raw.strip():
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as err:
        raise ValueError(
            f"MAILMAN_FILTERED_MEMBER_COUNTS is not valid JSON: {err}"
        ) from err
    if not isinstance(parsed, dict):
        raise ValueError(
            "MAILMAN_FILTERED_MEMBER_COUNTS must be a JSON object "
            '{"name": ["list_id", ...]}'
        )
    result: dict[str, list[str]] = {}
    for name, excluded in parsed.items():
        if not isinstance(excluded, list) or not all(
            isinstance(x, str) for x in excluded
        ):
            raise ValueError(
                f"MAILMAN_FILTERED_MEMBER_COUNTS['{name}'] must be a list of "
                "list_id strings"
            )
        result[str(name)] = excluded
    return result


class MailmanCollector:
    def __init__(self, dsn: str, filtered_member_counts: dict[str, list[str]] | None = None):
        self.dsn = dsn
        self.filtered_member_counts = filtered_member_counts or {}

    def collect(self):
        log.debug("starting scrape")
        start = time.monotonic()
        try:
            yield from self._collect()
        except Exception:
            log.exception("scrape failed")
            up = GaugeMetricFamily(
                "mailman_exporter_up", "Whether the Mailman exporter scrape is working"
            )
            up.add_metric([], 0)
            yield up
            return
        elapsed = time.monotonic() - start
        log.debug("scrape completed in %.3fs", elapsed)
        duration = GaugeMetricFamily(
            "mailman_scrape_duration_seconds", "Time taken to scrape Mailman DB"
        )
        duration.add_metric([], elapsed)
        yield duration

    def _collect(self):
        """Connect to DSN and collect metrics using SQL queries via _gauge helper."""
        with psycopg.connect(self.dsn, connect_timeout=10) as conn:
            conn.execute("SET default_transaction_read_only = on")
            conn.commit()

            yield from self._gauge(
                conn,
                "domains_total",
                "Number of configured mail domains",
                "SELECT count(*) FROM domain",
            )

            yield from self._gauge(
                conn,
                "lists_total",
                "Number of mailing lists",
                "SELECT mail_host, count(*) FROM mailinglist GROUP BY 1",
                labels=["domain"],
            )

            yield from self._gauge(
                conn,
                "members_total",
                "Number of memberships",
                "SELECT list_id, role, count(*) FROM member GROUP BY 1, 2",
                labels=["list_id", "role"],
                transform_labels=lambda row: [
                    row[0],
                    ROLE_NAMES.get(row[1], str(row[1])),
                ],
            )

            yield from self._gauge(
                conn,
                "users_total",
                "Total number of distinct users",
                'SELECT count(*) FROM "user"',
            )

            yield from self._gauge(
                conn,
                "pending_requests_total",
                "Pending moderation requests",
                """SELECT ml.list_id, r.request_type, count(*)
                   FROM _request r JOIN mailinglist ml ON r.mailing_list_id = ml.id
                   GROUP BY 1, 2""",
                labels=["list_id", "type"],
                transform_labels=lambda row: [
                    row[0],
                    REQUEST_TYPE_NAMES.get(row[1], str(row[1])),
                ],
            )

            yield from self._gauge(
                conn,
                "bouncing_members_total",
                "Members with bounce_score > 0",
                f"""SELECT list_id, count(*) FROM member
                    WHERE role = {MEMBER_ROLE_MEMBER} AND bounce_score > 0
                    GROUP BY 1""",
                labels=["list_id"],
            )

            yield from self._gauge(
                conn,
                "bounce_events_total",
                "Bounce events",
                "SELECT list_id, processed, count(*) FROM bounceevent GROUP BY 1, 2",
                labels=["list_id", "processed"],
                transform_labels=lambda row: [row[0], str(bool(row[1])).lower()],
            )

            yield from self._gauge(
                conn,
                "bans_total",
                "Number of bans",
                """SELECT CASE WHEN list_id IS NULL THEN 'site' ELSE 'list' END,
                          count(*) FROM ban GROUP BY 1""",
                labels=["scope"],
            )

            yield from self._gauge(
                conn,
                "header_matches_total",
                "Number of header match rules",
                "SELECT header, count(*) FROM headermatch GROUP BY 1",
                labels=["header"],
            )

            yield from self._gauge(
                conn,
                "content_filters_total",
                "Number of content filter rules",
                "SELECT count(*) FROM contentfilter",
            )

            yield from self._gauge(
                conn,
                "acceptable_aliases_total",
                "Number of acceptable alias entries",
                "SELECT count(*) FROM acceptablealias",
            )

            yield from self._gauge(
                conn,
                "lists_emergency_total",
                "Number of lists in emergency mode",
                "SELECT count(*) FROM mailinglist WHERE emergency = true",
            )

            yield from self._gauge(
                conn,
                "addresses_total",
                "Total email addresses",
                "SELECT count(*) FROM address",
            )

            yield from self._gauge(
                conn,
                "addresses_verified_total",
                "Verified email addresses",
                "SELECT count(*) FROM address WHERE verified_on IS NOT NULL",
            )

            yield from self._gauge(
                conn,
                "pending_tokens_total",
                "Pending confirmation tokens",
                "SELECT count(*) FROM pended WHERE expiration_date > now()",
            )

            yield from self._gauge(
                conn,
                "pending_tokens_expired_total",
                "Expired uncleaned pending tokens",
                "SELECT count(*) FROM pended WHERE expiration_date <= now()",
            )

            yield from self._gauge(
                conn,
                "messages_total",
                "Messages in message store",
                "SELECT count(*) FROM message",
            )

            yield from self._gauge(
                conn,
                "workflow_states_total",
                "Active workflow states",
                "SELECT step, count(*) FROM workflowstate GROUP BY 1",
                labels=["step"],
            )

            ## Possible postorius metrics, but not all db will have postorius enabled
            # auth_user: superusers?
            # django_session where expire_date > now(); active sessions?

            yield from self._list_timestamps(conn)

            yield from self._filtered_member_counts(conn)

        up = GaugeMetricFamily(
            "mailman_exporter_up", "Whether the Mailman exporter scrape is working"
        )
        up.add_metric([], 1)
        yield up

    def _gauge(self, conn, name, help_text, sql, labels=None, transform_labels=None):
        """Helper to create a GaugeMetricFamily from SQL query."""
        log.debug("collecting %s", name)
        g = GaugeMetricFamily(f"mailman_{name}", help_text, labels=labels or [])
        for row in conn.execute(sql).fetchall():
            if labels:
                label_vals = (
                    transform_labels(row)
                    if transform_labels
                    else [str(v) for v in row[:-1]]
                )
                g.add_metric(label_vals, row[-1])
            else:
                g.add_metric([], row[0])
        yield g

    def _list_timestamps(self, conn):
        """Collect last post and creation timestamps for each list."""
        log.debug("collecting list timestamps")

        last_post = GaugeMetricFamily(
            "mailman_list_last_post_timestamp",
            "Unix timestamp of last post to list (0 if never posted)",
            labels=["list_id"],
        )
        created = GaugeMetricFamily(
            "mailman_list_created_timestamp",
            "Unix timestamp of list creation",
            labels=["list_id"],
        )
        for list_id, last_post_at, created_at in conn.execute("""
            SELECT list_id,
                   extract(epoch FROM last_post_at),
                   extract(epoch FROM created_at)
            FROM mailinglist
        """).fetchall():
            ts = last_post_at or 0
            last_post.add_metric([list_id], ts)
            created.add_metric([list_id], created_at or 0)
        yield last_post
        yield created

    def _filtered_member_counts(self, conn):
        """Per-filter count of distinct verified members, excluding given lists.

        For each configured filter, count distinct member addresses that appear
        on at least one list NOT in the excluded set. An address whose only
        memberships are on excluded lists is not counted. This reproduces the
        legacy survey "exclude_listnames" semantics: filtering happens before
        the DISTINCT, so cross-membership is preserved.
        """
        if not self.filtered_member_counts:
            return

        g = GaugeMetricFamily(
            "mailman_filtered_members_total",
            "Distinct verified member addresses on at least one non-excluded list",
            labels=["filter"],
        )
        for name, excluded in self.filtered_member_counts.items():
            log.debug("collecting filtered_members filter=%s", name)
            row = conn.execute(
                """
                SELECT count(*) FROM (
                    SELECT DISTINCT a.email
                    FROM member m
                    JOIN address a ON m.address_id = a.id
                    WHERE m.role = %(role)s
                      AND a.verified_on IS NOT NULL
                      AND a.email ~ '@.+\\.'
                      AND m.list_id <> ALL(%(excluded)s)
                ) s
                """,
                {"role": MEMBER_ROLE_MEMBER, "excluded": excluded},
            ).fetchone()
            g.add_metric([name], row[0])
        yield g


def _build_dsn() -> str:
    """Build a PostgreSQL DSN from DB_* env vars, override by MAILMAN_DB_DSN."""
    if os.environ.get("MAILMAN_DB_DSN"):
        log.debug("Using MAILMAN_DB_DSN from env")
        return os.environ["MAILMAN_DB_DSN"]
    host = os.environ.get("DB_HOST", "localhost")
    port = os.environ.get("DB_PORT", "5432")
    name = os.environ.get("DB_NAME", "mailman")
    user = os.environ.get("DB_USER", "mailman")
    password = os.environ.get("DB_PASS", "")
    log.debug(
        "Building DSN from env vars: 'host=%s port=%s name=%s user=%s password=<redacted>'",
        host,
        port,
        name,
        user,
    )
    return make_conninfo(
        host=host, port=int(port), dbname=name, user=user, password=password
    )


class _LoggingMetricsHandler(MetricsHandler):
    def log_message(self, format, *args):
        log.info(format, *args)


def main():
    parser = argparse.ArgumentParser(description="Mailman 3 Prometheus exporter")
    parser.add_argument(
        "--dsn",
        default=None,
        help="PostgreSQL DSN (default: built from DB_HOST/DB_PORT/DB_NAME/DB_USER/DB_PASS env vars)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("MAILMAN_EXPORTER_PORT", "9934")),
        help="Port to listen on (default: $MAILMAN_EXPORTER_PORT or 9934)",
    )
    parser.add_argument(
        "--log-level",
        default=os.environ.get("MAILMAN_EXPORTER_LOG_LEVEL", "INFO"),
        help="Logging level (default: $MAILMAN_EXPORTER_LOG_LEVEL or INFO)",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )
    parser.add_argument(
        "--filtered-member-counts",
        default=os.environ.get("MAILMAN_FILTERED_MEMBER_COUNTS"),
        help=(
            "JSON object mapping a filter name to a list of list_ids to exclude, "
            'e.g. {"subscribers": ["allowlist.example.com"]}. Emits '
            "mailman_filtered_members_total{filter=...}. "
            "Default: $MAILMAN_FILTERED_MEMBER_COUNTS"
        ),
    )
    parser.add_argument(
        "--stdout",
        action="store_true",
        help="Print metrics to stdout and exit",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(message)s",
    )

    try:
        filtered_member_counts = _parse_filtered_member_counts(
            args.filtered_member_counts
        )
    except ValueError as err:
        log.error("%s", err)
        sys.exit(2)
    if filtered_member_counts:
        log.info(
            "filtered member counts configured: %s",
            ", ".join(filtered_member_counts),
        )

    dsn = args.dsn or _build_dsn()
    REGISTRY.register(MailmanCollector(dsn, filtered_member_counts))

    if args.stdout:
        sys.stdout.buffer.write(generate_latest(REGISTRY))
        return

    server = HTTPServer(("", args.port), _LoggingMetricsHandler)  # type: ignore[arg-type]
    log.info("started, listening on :%d", args.port)

    def _shutdown(signum, _frame):
        log.info("received %s, shutting down", signal.Signals(signum).name)
        threading.Thread(target=server.shutdown).start()

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    server.serve_forever()
    log.info("stopped")


if __name__ == "__main__":
    main()
