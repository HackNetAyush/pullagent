# Running CR as a public App on Azure

Publishing the App is what forces everything on this page. A private App can
only be installed by its owner, so a webhook arriving was itself proof that
somebody wanted it. A public App has no such proof: anyone can install it,
deliveries arrive with no human in the loop, and each one spends your model
budget. So the deployed service asks one question before anything costs money —
*is this account approved?* — and the rest of the architecture follows from
needing somewhere durable to keep that answer.

```
        GitHub ──webhook──► cr-web (always on, 0.5 vCPU)
                              │  verify → allowlist → enqueue → 202
                              ▼
                        Postgres (what is current)  ──►  Service Bus (who wakes up)
                                                              │
                                                              ▼
                                                        cr-worker (0 → 5 replicas)
                                                          claim → review → post
```

---

## What each piece is for

**Postgres** holds the truth: the allowlist, sessions, run history, suppression
memory, and the job table. `enqueue_job` supersedes older rows for a key at
write time, which is what makes five pushes cost one review.

**Service Bus** holds no state. Its messages say "look at job row N". A
duplicate delivery, a redelivery after a crash, and a message that lost its
race all converge on the same answer, because `claim_job` lets exactly one
worker move a row from `queued` to `running`. The app uses a Send/Listen key;
the scaler uses a separate Manage key scoped only to `cr-jobs` so it can read
queue depth. That stronger key is not mapped into the app process environment.

**Azure Files**, mounted at `/cache`, holds the git mirrors. This is the one
thing that genuinely needs a POSIX filesystem — git cannot maintain a mirror in
blob storage. The symbol graphs and the review cache live there too, because
everything under `CR_CACHE_DIR` already does and one mount is simpler than
three storage integrations.

**No Blob, no Redis.** Both were considered and neither earns its place yet.
The review cache is content-addressed files on the share; the token cache is
per-process and an hour long. Add Redis when the worker count makes repeated
Postgres reads show up in a trace, not before.

**Baseline cost is not zero.** The web Container App stays at one replica so
GitHub webhooks get a prompt response, and the Basic registry plus the small
PostgreSQL server run continuously. The worker scales to zero. Azure Files is
billed for used storage and transactions. Model calls are an additional
per-review cost. Check the Azure pricing calculator for the chosen region and
set a resource-group budget with email alerts before inviting other accounts.
An Azure budget sends alerts; it does not stop spending.

---

## Deploy it

### 1. Apply the infrastructure once with an Azure administrator

Create a resource group and Basic ACR first, build the dashboard and image,
then apply `infra/main.bicep` with a local, ignored parameters JSON file. The
template creates PostgreSQL, Service Bus, Azure Files, the Container Apps, and
their managed identity. The parameter file must supply the image, database
password, GitHub App credentials, Foundry key and resource name, and admin
login. Keep that file out of Git. The Container Apps hold their own secrets.
After the first apply returns the web ingress FQDN, set `publicUrl` to its
`https://` origin and reapply so GitHub OAuth redirects to the live site.

### 2. Federate GitHub Actions with narrow permissions

Create an Entra app with a federated credential whose subject is
`repo:OWNER/REPO:environment:production`. Give its service principal `AcrPush`
on the registry and `Container Apps Contributor` on **each of the two Container
Apps only**. It does not need Contributor on the resource group. Set these
**repository secrets**:

| Secret | What it is |
| --- | --- |
| `AZURE_CLIENT_ID` | the Entra app registration |
| `AZURE_TENANT_ID` | your tenant |
| `AZURE_SUBSCRIPTION_ID` | the subscription |

Set these **repository variables**:

| Variable | Example |
| --- | --- |
| `AZURE_RESOURCE_GROUP` | `pullagent-prod-eastus` |
| `ACR_NAME` | `crpa8b0eacr` |
| `APP_NAME` | `crpa8b0e` |

GitHub Actions never receives the App private key, webhook secret, database
password, or model key.

### 3. Push to `main`

`.github/workflows/deploy.yml` builds and pushes the image, updates the worker
and web Container Apps, and checks `/api/health`. Infrastructure changes are
applied with Bicep under an Azure administrator login. The job summary prints
the webhook URL.

### 4. Point the App at it

In the App's settings, set the webhook URL to the address from the summary
(`https://<fqdn>/webhook`) and the callback URL to `https://<fqdn>/auth/callback`.
The smee relay is no longer involved.

### 5. Make yourself an administrator, then decide which installations to approve

`CR_ADMIN_LOGINS` promotes you on your **first sign-in**, so visit the
dashboard and sign in before anything else. Approval is account-wide: it can
enable reviews in every repository included in that account's GitHub App
installation. Narrow the installation to selected repositories in GitHub
before approving the account if you want to bound model costs. Then:

```bash
cr app approve <login>             # enables the selected account's installation
cr app accounts                    # check
```

Without this the App is deployed and reviewing nothing, which is the correct
failure but an alarming one if you are not expecting it.

---

## Turning on the allowlist without breaking existing users

`CR_APP_REQUIRE_APPROVAL` defaults to **on**, because the cost of forgetting it
on a public App is a stranger spending your budget, while the cost of it being
on unnecessarily is one command. The consequence is that upgrading an existing
installation stops reviews until an account is approved.

`cr app approve --all-installed` is available when you have reviewed every
installed account and want them all to continue. `check_ready()` also prints a warning at start-up
when approval is required and nothing is approved, so the failure announces
itself rather than looking like an outage.

---

## Operating it

```bash
cr app accounts                 # who is approved, denied, or waiting
cr app approve <login>          # let an account in
cr app deny <login>             # and back out again
cr app admin <login>            # promote someone who has signed in once
cr app status                   # installations, recent jobs, configuration
```

An account that is not approved still shows up: the ingress counts its dropped
deliveries in `blocked_events`, so `cr app accounts` distinguishes "nobody has
asked" from "somebody is knocking and getting nothing".

---

## What this deployment does not do

**A running review cannot be cancelled across replicas.** In-process, a newer
push cancels the asyncio task outright. Here the older review runs to
completion and is stopped at the last moment by `job_is_current()`, immediately
before it posts. The work is wasted; the output is never wrong.

**Debounce is a scheduled message, not a sliding window.** Five pushes schedule
five messages and four of them find a superseded row. Each costs one Postgres
read, which is the trade for not holding a timer in a process that may vanish.

**Postgres is reachable from Azure services generally.** The firewall rule is
`AllowAllAzureServices`, because Container Apps egress addresses are not fixed.
VNet integration is the upgrade when that stops being acceptable; it is a
change to `infra/main.bicep` and nothing else.

**The app and database can be in different Azure regions.** The `pgLocation`
parameter exists because this subscription permits the B1ms database in
Central US but restricts it in East US. This adds some database latency and
small cross-region transfer charges. Keep both in one region when the
subscription allows it.
