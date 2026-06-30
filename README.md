# mailman-db-exporter

Standalone Prometheus exporter for Mailman 3. Reads metrics directly from PostgreSQL for speed
instead of the REST API.

Exposes ~20 metrics covering lists, members, bounces, moderation queues, bans, workflow states,
and per-list last-post timestamps.

## Quick start

### Docker / Podman

```bash
docker run -d --name mailman-exporter \
  -e DB_HOST=your-db-host \
  -e DB_PORT=5432 \
  -e DB_NAME=mailman \
  -e DB_USER=mailman \
  -e DB_PASS=secret \
  -p 9934:9934 \
  ghcr.io/sargeant/mailman-db-exporter:latest

curl http://localhost:9934/metrics
```

### Helm

```bash
helm repo add mailman-db-exporter https://sargeant.github.io/mailman-db-exporter
helm install mailman-exporter mailman-db-exporter/mailman-db-exporter \
  --set database.host=mailman-db \
  --set database.password=secret \
  --set serviceMonitor.enabled=true \
  --set serviceMonitor.labels.release=kube-prometheus-stack
```

## Configuration

### Environment variables

| Variable | Default | Description |
|---|---|---|
| `DB_HOST` | `localhost` | PostgreSQL host |
| `DB_PORT` | `5432` | PostgreSQL port |
| `DB_NAME` | `mailman` | Database name |
| `DB_USER` | `mailman` | Database user |
| `DB_PASS` | (empty) | Database password |
| `MAILMAN_DB_DSN` | (unset) | Full DSN, overrides individual `DB_*` vars |
| `MAILMAN_EXPORTER_PORT` | `9934` | Port to listen on |
| `MAILMAN_EXPORTER_LOG_LEVEL` | `INFO` | Log level (`DEBUG`, `INFO`, `WARNING`, `ERROR`) |
| `MAILMAN_FILTERED_MEMBER_COUNTS` | (unset) | JSON config for filtered member counts (see below) |

### Filtered member counts

`MAILMAN_FILTERED_MEMBER_COUNTS` defines named subscriber counts that exclude
specific lists. It's a JSON object mapping a filter name to the `list_id`s to
exclude:

```json
{"subscribers": ["allowlist.example.com", "surveys.example.com"]}
```

Each entry emits one series:

```
mailman_filtered_members_total{filter="subscribers"} 42137
```

The count is **distinct verified member addresses that belong to at least one
list other than the excluded ones**. An address whose only memberships are on
excluded lists is not counted; an address on an excluded list *and* some other
list still is. This matches the "how many real subscribers do we have" question
where housekeeping lists (a global allowlist, a survey list) shouldn't inflate
the number.

### Helm values

See [values.yaml](charts/mailman-db-exporter/values.yaml). Key settings:

- `database.existingSecret` — reference a pre-existing Secret containing `DB_HOST`, `DB_PORT`, `DB_NAME`, `DB_USER`, `DB_PASS` keys (ignores inline `database.*` fields when set)
- `serviceMonitor.enabled` — creates a Prometheus Operator ServiceMonitor
- `serviceMonitor.labels` — must match your Prometheus Operator's selector (e.g. `release: kube-prometheus-stack`)

## Security

Create a dedicated **read-only** PostgreSQL user for the exporter. It only runs `SELECT` queries — don't reuse the Mailman application user.

## Metrics

All metrics are prefixed with `mailman_`. Key ones worth alerting on:

- `mailman_exporter_up == 0` — exporter or DB connection broken
- `mailman_lists_emergency_total > 0` — a list is in emergency mode
- `mailman_pending_requests_total{type="held_message"}` growing — moderation queue may be abandoned
- `mailman_bouncing_members_total` sudden spike — likely a delivery infrastructure problem
- `mailman_pending_tokens_expired_total` growing — `mailman purge_pended` isn't running
- `mailman_workflow_states_total` growing — subscription pipeline is jammed
