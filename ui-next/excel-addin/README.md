# Atlas for Excel — setup

The add-in puts a **Save to Atlas** button on Excel's Home tab. From the pane
you can open a datasource's model workbook straight from Atlas, edit it in
Excel, and save it back. Saving creates the same draft batch a browser upload
creates: nothing is published until someone other than you approves it in the
review queue, and an edit to anything published after you opened the workbook
is skipped rather than written over.

It works in Excel for Windows, Excel for Mac and Excel on the web (ExcelApi 1.8
or later, which is any Microsoft 365 Excel).

## Steps only you can do

Four of these change your machine, your identity provider or your Microsoft 365
tenant, so they are yours to run.

### 1. Trust a localhost HTTPS certificate (once per machine)

Office loads add-in pages only over HTTPS, localhost included. Create and
trust a development certificate:

```bash
npx office-addin-dev-certs install
```

Windows asks you to confirm installing the certificate authority. The files
land in `%USERPROFILE%\.office-addin-dev-certs\`.

### 2. Run the dev server with HTTPS and a live API

The pane talks to the real API: the demo fixtures cannot parse a workbook.
From the repository root, in PowerShell:

```powershell
$env:VITE_DEV_HTTPS_CERT = "$env:USERPROFILE\.office-addin-dev-certs\localhost.crt"
$env:VITE_DEV_HTTPS_KEY  = "$env:USERPROFILE\.office-addin-dev-certs\localhost.key"
$env:VITE_USE_FIXTURES   = "0"
npm --prefix ui-next run dev
```

Check `https://localhost:5174/excel-addin.html` opens in a browser without a
certificate warning. It should say the page runs inside Excel.

With neither `VITE_DEV_HTTPS_*` variable set, the dev server stays on plain
HTTP exactly as before.

### 3. Load the add-in into Excel (once)

- **Excel on the web or Microsoft 365 desktop:** Home → Add-ins → More Add-ins
  → My Add-ins → *Upload My Add-in* → choose `ui-next/excel-addin/manifest.xml`.
- **Excel desktop without that option:** share a folder containing the
  manifest, add it under File → Options → Trust Center → Trust Center Settings
  → Trusted Add-in Catalogs, restart Excel, then Home → Add-ins → Shared Folder.

An **Atlas** group with **Save to Atlas** appears on the Home tab.

### 4. Identity provider (only if the build signs in with OIDC)

In development mode the pane uses the development principal, the same as the
browser app, and this step does not apply. Under OIDC, register this redirect
URI for the ui-next client at your identity provider:

```
https://localhost:5174/excel-addin-auth.html
```

For another host, register `<that origin>/excel-addin-auth.html`.

## Using it

1. Open the pane (Home → Atlas → Save to Atlas).
2. **Open a model from Atlas**: choose a source, then *Open in Excel*. The
   workbook opens in a new window. Open the pane there too.
3. Edit `business_description` (and the table fields the README sheet lists).
   `drafted_description` holds any machine draft. It is read-only; copy it
   into `business_description` to adopt it, fixing it as you go.
4. **Save to Atlas**. The pane reports how many changes are ready and how many
   rows could not be applied. *Check the changes row by row in Atlas* opens the
   same preview the Sources screen shows, where rows can be excluded.
5. **Submit for review**. Someone other than you approves it in the review
   queue.

A workbook can only be saved into the source it was exported from: the server
checks the README sheet's `Datasource id` and refuses anything else.

## Deploying beyond localhost

1. Build and host `ui-next` over HTTPS (the production nginx image already
   serves `excel-addin.html`, `excel-addin-auth.html` and
   `/excel-addin/*.png` from the build).
2. Copy `manifest.xml` and replace every `https://localhost:5174` with the real
   origin. `tests/test_excel_addin_manifest.py` checks all URLs share one
   HTTPS origin. Give the copy a new `<Id>` if it should be installable
   alongside the development one.
3. Register `<origin>/excel-addin-auth.html` with the identity provider.
4. A Microsoft 365 admin deploys the manifest to users under Integrated apps in
   the Microsoft 365 admin center.

## What is not verified yet

The add-in's logic is covered by unit tests against a fake Office runtime, and
the manifest is checked against the build. Neither has been loaded in a real
Excel yet, and nobody has signed in through a real identity provider from the
dialog. Steps 1–3 above are that test.
