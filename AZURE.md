# Azure App Service for Containers — deploy guide

Azure builds and runs **this repo's `Dockerfile` directly**. No separate repo, no
staging folder, no README frontmatter — `hf-space/` is only for the Hugging Face path
and is irrelevant here.

The dashboard still goes to Vercel (`dashboard/` as the project root); only the API is
described below.

## Fixed vs. placeholder values

Get this right and the rest follows. **Fixed** values come from this codebase — do not
change them. **Placeholders** are yours to choose; every one is written as
`<ANGLE-BRACKETS>` in the commands below.

| Value | Fixed or yours | Notes |
|---|---|---|
| `WEBSITES_PORT` = **`7860`** | **Fixed** | Must match the container's listening port, which is `ENV PORT=7860` in the `Dockerfile`. Azure routes external traffic here. |
| `TRIAGE_READ_ONLY` = **`true`** | **Fixed** | Already baked into the image's `ENV`; setting it explicitly is belt-and-braces. |
| Dockerfile path = **`./Dockerfile`** | **Fixed** | Repo root. |
| Health check path = **`/health`** | **Fixed** | Returns `{"status":"ok", …, "read_only":true}`. |
| `<RESOURCE-GROUP>` | Yours | e.g. `triage-rg` |
| `<APP-NAME>` | Yours | Globally unique; becomes `https://<APP-NAME>.azurewebsites.net` |
| `<REGISTRY-NAME>` | Yours | ACR name, globally unique, alphanumeric only |
| `<LOCATION>` | Yours | e.g. `centralindia`, `southeastasia` |
| `<PLAN-NAME>` | Yours | e.g. `triage-plan` |
| `<ALLOWED-ORIGIN>` | Yours | Your Vercel URL, e.g. `https://triage-xyz.vercel.app` — not known until the dashboard is deployed |

**One prerequisite worth knowing up front:** App Service for Containers deploys a
**container image**, not source code. Either path therefore needs a registry (Azure
Container Registry is the path of least resistance) as the intermediary — GitHub
integration builds the image and pushes it there, and App Service pulls from it. There
is no "point at a repo and it runs the Dockerfile" mode that skips the registry.

Note that `--sku B1` is a **paid** Basic tier (roughly $13/month). It's what you asked
for; mentioning the cost only because earlier deploy targets in this project were
chosen to be free.

---

## Path A — Deployment Center (portal, no CLI)

**1. Create the container registry** (once)
Portal → **Container registries** → **Create** → pick `<RESOURCE-GROUP>`, name
`<REGISTRY-NAME>`, SKU **Basic** → Create.
Then open the registry → **Access keys** → enable **Admin user** (App Service uses it
to pull).

**2. Create the Web App**
Portal → **App Services** → **Create** → **Web App**.
- Resource Group: `<RESOURCE-GROUP>`
- Name: `<APP-NAME>`
- **Publish: Container**
- **Operating System: Linux**
- Region: `<LOCATION>`
- Pricing plan: **B1**
- On the **Container** tab, pick any placeholder image for now (e.g. the default
  sample) — Deployment Center overwrites it in the next step.
- Create.

**3. Wire up GitHub in Deployment Center**
Web App → **Deployment Center**:
- **Source: GitHub** → authorise → Organization `Aryan27-max`, Repository `TRIAGE`,
  Branch `main`
- **Registry: Azure Container Registry** → select `<REGISTRY-NAME>`, image name e.g.
  `triage-api`, tag `latest`
- **Dockerfile location:** `Dockerfile` (repo root — leave the default `./` context)
- **Save.**

This commits a GitHub Actions workflow to your repo that builds the `Dockerfile` and
pushes to ACR on every push to `main`; App Service then pulls the new image.

**4. Set the application settings** — the step everything hinges on
Web App → **Configuration** → **Application settings** → **New application setting**,
add both, then **Save** (the app restarts):

| Name | Value |
|---|---|
| `WEBSITES_PORT` | `7860` |
| `TRIAGE_READ_ONLY` | `true` |

Add `ALLOWED_ORIGINS` = `<ALLOWED-ORIGIN>` here too, once the Vercel domain exists.

**Without `WEBSITES_PORT=7860` the container starts but every request times out** —
Azure defaults to probing 80/8080 while uvicorn is listening on 7860. That mismatch is
the single most common failure on this platform, and the logs make it look like the app
crashed when it didn't.

**5. Verify**
`https://<APP-NAME>.azurewebsites.net/health` → `200`, `"read_only": true`.
First pull after a deploy is slow; if it 503s immediately, give it a minute.

---

## Path B — Azure CLI

Placeholders as per the table above. `az acr build` builds **in the cloud**, so a
working local Docker daemon is not required.

```bash
# 0. Sign in and select a subscription
az login

# 1. Resource group
az group create \
  --name <RESOURCE-GROUP> \
  --location <LOCATION>

# 2. Container registry, and build the image from this repo's Dockerfile.
#    Run this from the repo root — the final "." is the build context.
az acr create \
  --resource-group <RESOURCE-GROUP> \
  --name <REGISTRY-NAME> \
  --sku Basic \
  --admin-enabled true

az acr build \
  --registry <REGISTRY-NAME> \
  --image triage-api:latest \
  --file Dockerfile \
  .

# 3. App Service plan — Linux, B1
az appservice plan create \
  --name <PLAN-NAME> \
  --resource-group <RESOURCE-GROUP> \
  --is-linux \
  --sku B1

# 4. Web app, pointed at the image just built
az webapp create \
  --resource-group <RESOURCE-GROUP> \
  --plan <PLAN-NAME> \
  --name <APP-NAME> \
  --deployment-container-image-name <REGISTRY-NAME>.azurecr.io/triage-api:latest

# 5. Let the web app pull from the registry
az webapp config container set \
  --name <APP-NAME> \
  --resource-group <RESOURCE-GROUP> \
  --container-image-name <REGISTRY-NAME>.azurecr.io/triage-api:latest \
  --container-registry-url https://<REGISTRY-NAME>.azurecr.io \
  --container-registry-user <REGISTRY-NAME> \
  --container-registry-password "$(az acr credential show --name <REGISTRY-NAME> --query 'passwords[0].value' -o tsv)"

# 6. Application settings. WEBSITES_PORT and TRIAGE_READ_ONLY are fixed values;
#    ALLOWED_ORIGINS is yours (set it once the Vercel domain exists).
az webapp config appsettings set \
  --resource-group <RESOURCE-GROUP> \
  --name <APP-NAME> \
  --settings WEBSITES_PORT=7860 \
             TRIAGE_READ_ONLY=true \
             ALLOWED_ORIGINS=<ALLOWED-ORIGIN>

# 7. Health check path (optional but recommended)
az webapp config set \
  --resource-group <RESOURCE-GROUP> \
  --name <APP-NAME> \
  --health-check-path /health

# 8. Verify
curl https://<APP-NAME>.azurewebsites.net/health
```

**Redeploying after a code change:** re-run step 2's `az acr build`, then
`az webapp restart --name <APP-NAME> --resource-group <RESOURCE-GROUP>`.

---

## After either path

1. Deploy the dashboard to Vercel (root directory `dashboard`) and set
   `NEXT_PUBLIC_API_URL` = `https://<APP-NAME>.azurewebsites.net`.
2. Come back and set `ALLOWED_ORIGINS` on the Web App to the Vercel domain. Until you
   do, the API allows `*` (its unset default), which works but is not what you want on
   a public origin.
3. Confirm end to end: the dashboard's **Live** screen should resolve a decision with
   no CORS error in the browser console.

## Notes specific to this image

- **Nothing is written at runtime.** The two evaluation runs and the trained model are
  `COPY`'d into the image at build time and served read-only, so no Azure Files mount
  and no persistent storage is needed. Leave `WEBSITES_ENABLE_APP_SERVICE_STORAGE` at
  its default (`false`).
- **`PORT` on Azure.** The image sets `ENV PORT=7860` and the `CMD` is shell form, so
  `${PORT}` expands at container start. If Azure injects its own `PORT`, uvicorn honours
  it; if it doesn't, the baked default applies. Either way the container ends up on
  7860 as long as `WEBSITES_PORT=7860` agrees with it — which is why that setting is
  non-negotiable rather than merely recommended.
- **Container logs**, if it won't start:
  `az webapp log tail --name <APP-NAME> --resource-group <RESOURCE-GROUP>`
