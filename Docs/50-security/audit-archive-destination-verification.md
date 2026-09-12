# Verifying the WORM audit archive against a real destination

**What this document is for.** The audit archive's value rests on four claims:
the bytes reached a destination, the destination hands them back unchanged, the
destination refuses to delete them before retention elapses, and a legal hold
outranks retention. Three of those are claims about somebody else's
infrastructure, and no test in this repository can make them about *your*
bucket. This is the procedure that does.

Run it once per environment, after the bucket is created and before the archive
is relied on for anything. Record the date and the operator in the capability
register's F01 row.

---

## 0. What is already proven, and where

Do not re-prove these by hand; they run in CI.

| Property | Proven by | Against what |
| --- | --- | --- |
| Lifecycle reaches VERIFIED only through a destination that took the bytes | `tests/test_worm_archive_lifecycle.py`, `tests/test_worm_archive_wiring.py` | Local filesystem provider |
| An unconfigured destination fails rather than reporting success | `tests/test_worm_archive_lifecycle.py::test_unconfigured_destination_fails_instead_of_reporting_success` | `NullArchiveStorage` |
| S3 read-back, version pinning, COMPLIANCE refusal, legal hold — **driven through `archive_pending_audit_events`** | `tests/test_audit_archive_s3_readback.py` | In-memory Object Lock service (`tests/support/s3_object_lock_double.py`) exercising the real `S3ArchiveStorage` over a substituted `httpx` transport |
| The same four properties against a live S3-compatible service | `tests/test_audit_archive_s3.py` (live block) | MinIO, when one is running; **skipped otherwise, including in CI** |

The third row is the one added by R11-B9. It closed the case where CI covered
the S3 provider not at all, because the live tests skip there. It does **not**
replace this document: the double implements S3's *documented* Object Lock
semantics, and a real bucket can fail to enforce them for reasons the double
cannot model — Object Lock never enabled at creation, an IAM policy granting
`s3:BypassGovernanceRetention`, a bucket policy permitting version deletion, or
a provider whose Object Lock support is partial.

---

## 1. Against MinIO (the local stack)

This is the cheap rehearsal. It proves the provider speaks the protocol to a
real implementation, and it is what the 2026-09-09 F01 evidence recorded.

```bash
docker compose up -d minio
```

Then, with the repository's virtualenv active and `AIDA_ENVIRONMENT` unset:

```bash
pytest tests/test_audit_archive_s3.py -v
```

Every test in the `--- live MinIO ---` block must **pass**, not skip. A skip
means no object store was reachable and the run proved nothing; the skip message
names the endpoint it tried. Point it elsewhere with:

```bash
AIDA_OBJECT_STORE_ENDPOINT=http://localhost:9000 \
AIDA_OBJECT_STORE_ACCESS_KEY=... \
AIDA_OBJECT_STORE_SECRET_KEY=... \
pytest tests/test_audit_archive_s3.py -v
```

**What this still does not prove.** MinIO refuses a locked delete with
`400 InvalidRequest`; AWS refuses it with `403 AccessDenied`, and on AWS the
refusal can also come from IAM or bucket policy rather than from Object Lock.
A green MinIO run is necessary and not sufficient.

---

## 2. Against the real bucket

Run every step against the bucket the deployment will actually use, with the
credentials the application will actually use. Using an administrator's
credentials proves the bucket is lockable, not that the application is confined
— which is the claim that matters.

Export the deployment's own settings first:

```bash
export AIDA_OBJECT_STORE_ENDPOINT=https://s3.eu-west-1.amazonaws.com
export AIDA_OBJECT_STORE_ACCESS_KEY=...      # the application's key
export AIDA_OBJECT_STORE_SECRET_KEY=...      # the application's secret
export AIDA_AUDIT_ARCHIVE_BUCKET_NAME=...
export AIDA_AUDIT_ARCHIVE_S3_REGION=eu-west-1
```

### 2.1 Object Lock is enabled on the bucket

Object Lock can only be turned on when a bucket is created. A bucket without it
accepts every write and retains nothing, which looks identical to success.

```bash
aws s3api get-object-lock-configuration --bucket "$AIDA_AUDIT_ARCHIVE_BUCKET_NAME"
```

**Expected:** `ObjectLockEnabled: Enabled`. A `ObjectLockConfigurationNotFoundError`
means this bucket can never be a WORM destination and must be recreated.

### 2.2 The application writes, and the write is locked

Let one real sweep run — or drive one deliberately:

```bash
python -c "
import asyncio
from aida.worm_archive import archive_pending_audit_events, ArchiveConfig
from atlas.platform.config import get_settings
# ... open a session against the deployment database, then:
# asyncio.run(archive_pending_audit_events(session, org_id, config))
"
```

Then take the `storage_uri` from the newest `audit_archive_record` row whose
`state = 'VERIFIED'` and split it into key and version:

```sql
SELECT archive_id, storage_uri, checksum, retention_until, state
FROM audit_archive_record
WHERE state = 'VERIFIED'
ORDER BY created_at DESC
LIMIT 1;
```

```bash
aws s3api head-object \
  --bucket "$AIDA_AUDIT_ARCHIVE_BUCKET_NAME" \
  --key "<organization_id>/<archive_id>.json" \
  --version-id "<versionId from the uri>"
```

**Expected:** `ObjectLockMode: COMPLIANCE` and an `ObjectLockRetainUntilDate`
matching `retention_until` on the row. A response with no lock fields means the
object was written unlocked and is not retained.

### 2.3 The bytes come back unchanged

```bash
aws s3api get-object \
  --bucket "$AIDA_AUDIT_ARCHIVE_BUCKET_NAME" \
  --key "<organization_id>/<archive_id>.json" \
  --version-id "<versionId>" /tmp/archive.json

python -c "import hashlib,sys; print(hashlib.sha256(open('/tmp/archive.json','rb').read()).hexdigest())"
```

**Expected:** the digest equals the object's `x-amz-meta-payload-sha256`
metadata (visible in the `head-object` output above). Also confirm the document
is self-describing — it must carry its own `checksum` and `event_count`:

```bash
python -c "
import json; d=json.load(open('/tmp/archive.json'))
print(d['archive_id'], d['checksum'], d['event_count'], len(d['events']))
"
```

**Expected:** the `checksum` matches the `checksum` column on the row, and
`event_count == len(events)`.

### 2.4 The destination refuses to delete it

This is the property the whole archive exists for, and the only way to prove it
is to try.

```bash
aws s3api delete-object \
  --bucket "$AIDA_AUDIT_ARCHIVE_BUCKET_NAME" \
  --key "<organization_id>/<archive_id>.json" \
  --version-id "<versionId>"
```

**Expected on AWS:** `An error occurred (AccessDenied) when calling the
DeleteObject operation`. **Expected on MinIO:** `400 InvalidRequest`, "Object is
WORM protected".

**A success here is a failed verification.** It means retention is not in force
— most often because the object was written before Object Lock was configured,
or because the calling identity holds `s3:BypassGovernanceRetention` and the
bucket is in GOVERNANCE rather than COMPLIANCE mode. Do not proceed.

### 2.5 An overwrite cannot destroy the archived version

```bash
echo '{}' > /tmp/overwrite.json
aws s3api put-object \
  --bucket "$AIDA_AUDIT_ARCHIVE_BUCKET_NAME" \
  --key "<organization_id>/<archive_id>.json" \
  --body /tmp/overwrite.json

aws s3api get-object \
  --bucket "$AIDA_AUDIT_ARCHIVE_BUCKET_NAME" \
  --key "<organization_id>/<archive_id>.json" \
  --version-id "<versionId>" /tmp/still-there.json
```

**Expected:** the overwrite succeeds (it creates a *new* version) and the
original version is still retrievable and still hashes to the same digest as in
2.3. If the second command fails, the bucket is not versioned and Object Lock
cannot be in force.

### 2.6 A legal hold outranks retention

```bash
aws s3api put-object-legal-hold \
  --bucket "$AIDA_AUDIT_ARCHIVE_BUCKET_NAME" \
  --key "<organization_id>/<archive_id>.json" \
  --version-id "<versionId>" \
  --legal-hold Status=ON

aws s3api get-object-legal-hold \
  --bucket "$AIDA_AUDIT_ARCHIVE_BUCKET_NAME" \
  --key "<organization_id>/<archive_id>.json" \
  --version-id "<versionId>"
```

**Expected:** `Status: ON`. Proving that a hold *outlives retention* requires an
object whose `retain-until` has passed, which for the shipped 2555-day default
is not reachable in a verification window. Two honest options, in order of
preference:

1. Write a throwaway archive to the same bucket with a short retention
   (`AIDA_AUDIT_ARCHIVE_RETENTION_DAYS=1`) under a scratch organization id,
   wait out the day, and then run 2.4 with the hold on and again with it off.
2. Record that the hold-outlives-retention property is proven against the
   Object Lock *semantics* (`tests/test_audit_archive_s3_readback.py::test_legal_hold_outlives_retention_and_release_restores_expiry`)
   and against MinIO, and is **not** proven against this bucket. This is a
   legitimate answer; asserting it without option 1 is not.

Release the hold afterwards, or the scratch object is undeletable forever:

```bash
aws s3api put-object-legal-hold ... --legal-hold Status=OFF
```

### 2.7 The application's own identity cannot bypass any of it

Repeat 2.4 using the application's credentials rather than an operator's, if you
did not already. Then confirm the deployment's IAM policy does **not** grant:

- `s3:BypassGovernanceRetention`
- `s3:PutObjectRetention` (the application never needs to shorten retention)
- `s3:PutBucketObjectLockConfiguration`
- `s3:DeleteObjectVersion`

```bash
aws iam simulate-principal-policy \
  --policy-source-arn "<the application's role arn>" \
  --action-names s3:BypassGovernanceRetention s3:DeleteObjectVersion \
  --resource-arns "arn:aws:s3:::$AIDA_AUDIT_ARCHIVE_BUCKET_NAME/*"
```

**Expected:** `implicitDeny` for each.

---

## 3. Recording the result

Update the F01 row in
[`Docs/60-delivery/20-capability-register.md`](../60-delivery/20-capability-register.md)
with the date, the service actually used (AWS S3 / MinIO / other), and which of
2.1–2.7 passed. A row that says "Verified: Yes" without naming the service and
the date is the exact kind of claim this register exists to prevent.

If 2.6 was answered with option 2, say so in the row. "Verified except the
hold-outlives-retention property, which is proven against Object Lock semantics
and MinIO but not against this bucket" is a useful sentence. "Verified" is not.
