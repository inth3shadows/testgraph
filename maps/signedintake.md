# Journey map — which user journeys depend on which symbols

Target: `/home/ericm/personal_projects/signedintake/main` · index schema 8 · generated from commit `0305ada`

> **The index was not fully trustworthy when this map was generated.** These warnings were raised at generation time and are reproduced here because this file outlives the run that made it. A stale or unverified index makes the map *under-report* — a symbol missing from it may still reach journeys.
>
> - 81 source file(s) newer than the index (e.g. drizzle.config.ts) — consider `codegraph sync`

Look up the symbols you changed **by name**. Every journey listed for them may have changed behavior and is worth verifying. This is **recall-first**: a shared symbol legitimately fans out to many journeys.

**Line numbers are frozen at the commit above and are a hint only** — your own edit has already shifted them, so an insertion higher up the file makes the ranges point at the wrong symbol (issue #24). Match the symbol name first, and fall back to the range when you cannot: import nodes and module-level bindings get rows too, and an edit to one of those looks like no symbol you touched. An edit you cannot attribute to any row is *unknown*, never *no journeys*.

`!` marks a journey reached only through weak or synthesized graph edges — treat it as *verify manually*, not as *probably fine*.

## Journeys

- **J1** claimant submits a signed form — entry: `submitForm`, `Page`
- **J2** staff issues a form link — entry: `issueFormLink`
- **J3** onboarding: form created from YAML — entry: `validateAndPreview`, `createFormAndLink`
- **J4** staff reviews a submission — entry: `SubmissionDetailPage`, `StaffIndexPage`
- **J5** staff requests a payment update — entry: `createPaymentUpdateRequest`, `simulatePaymentCompletion`
- **J6** stripe webhook settles a payment — entry: `POST`
- **J7** client polls payment status — entry: `GET`
- **J8** staff views a form's detail — entry: `FormDetailPage`

## Symbols by file

### `src/app/api/payment-requests/[requestId]/status/route.ts`

| symbol | journeys | lines (at generation — stale hint) |
|---|---|---|
| `next/server` | J7 | 1–1 |
| `@/db` | J7 | 2–2 |
| `@/db/schema` | J7 | 3–3 |
| `drizzle-orm` | J7 | 4–4 |
| `GET` | J7 | 8–28 |

### `src/app/api/webhooks/stripe/route.ts`

| symbol | journeys | lines (at generation — stale hint) |
|---|---|---|
| `next/server` | J6 | 5–5 |
| `@/db` | J6 | 6–6 |
| `@/db/schema` | J6 | 7–7 |
| `drizzle-orm` | J6 | 8–8 |
| `@/lib/payments` | J6 | 9–9 |
| `@/lib/payments/stripe` | J6 | 10–10 |
| `stripe` | J6 | 11–11 |
| `POST` | J6 | 13–94 |

### `src/app/f/[token]/FillForm.tsx`

| symbol | journeys | lines (at generation — stale hint) |
|---|---|---|
| `react` | J1 | 3–3 |
| `./actions` | J1 | 4–4 |
| `@/components/FormRenderer` | J1 | 5–5 |
| `@/lib/form-def` | J1 | 6–6 |
| `FillFormProps` | J1 | 8–11 |
| `FillForm` | J1 | 15–83 |

### `src/app/f/[token]/actions.ts`

| symbol | journeys | lines (at generation — stale hint) |
|---|---|---|
| `LinkAlreadyUsedError` | J1 | 31–31 |
| `SubmitResult` | J1 | 33–33 |
| `submitForm` | J1 | 35–383 |

### `src/app/f/[token]/page.tsx`

| symbol | journeys | lines (at generation — stale hint) |
|---|---|---|
| `./FillForm` | J1 | 7–7 |
| `Page` | J1 | 9–106 |

### `src/app/onboarding/actions.ts`

| symbol | journeys | lines (at generation — stale hint) |
|---|---|---|
| `validateAndPreview` | J3 | 9–29 |
| `createFormAndLink` | J3 | 31–78 |

### `src/app/staff/AddPasskeyButton.tsx`

| symbol | journeys | lines (at generation — stale hint) |
|---|---|---|
| `react` | J4 | 3–3 |
| `next/navigation` | J4 | 4–4 |
| `next-auth/webauthn` | J4 | 5–5 |
| `AddPasskeyButton` | J4 | 11–54 |

### `src/app/staff/LocalTime.tsx`

| symbol | journeys | lines (at generation — stale hint) |
|---|---|---|
| `formatUtc` | J4 J8 | 10–18 |
| `LocalTime` | J4 J8 | 26–36 |

### `src/app/staff/[submissionId]/PaymentSection.tsx`

| symbol | journeys | lines (at generation — stale hint) |
|---|---|---|
| `./payment-actions` | J4 | 4–4 |
| `../LocalTime` | J4 | 5–5 |
| `Props` | J4 | 15–23 |
| `PaymentSection` | J4 | 75–141 |

### `src/app/staff/[submissionId]/page.tsx`

| symbol | journeys | lines (at generation — stale hint) |
|---|---|---|
| `./PaymentSection` | J4 | 9–9 |
| `../LocalTime` | J4 | 10–10 |
| `SubmissionDetailPage` | J4 | 12–223 |

### `src/app/staff/[submissionId]/payment-actions.ts`

| symbol | journeys | lines (at generation — stale hint) |
|---|---|---|
| `@/db` | J4 J5 | 3–3 |
| `@/db/schema` | J4 J5 | 4–4 |
| `drizzle-orm` | J4 J5 | 5–5 |
| `@/lib/session` | J4 J5 | 6–6 |
| `@/lib/payments` | J4 J5 | 7–7 |
| `@/lib/base-url` | J4 J5 | 8–8 |
| `createPaymentUpdateRequest` | J4 J5 | 10–72 |
| `simulatePaymentCompletion` | J5 | 74–110 |

### `src/app/staff/forms/CopyButton.tsx`

| symbol | journeys | lines (at generation — stale hint) |
|---|---|---|
| `CopyButton` | J8 | 8–38 |

### `src/app/staff/forms/GenerateLinkButton.tsx`

| symbol | journeys | lines (at generation — stale hint) |
|---|---|---|
| `./actions` | J8 | 5–5 |
| `./CopyButton` | J8 | 6–6 |
| `GenerateLinkButton` | J8 | 10–64 |

### `src/app/staff/forms/[formId]/page.tsx`

| symbol | journeys | lines (at generation — stale hint) |
|---|---|---|
| `../GenerateLinkButton` | J8 | 7–7 |
| `../CopyButton` | J8 | 8–8 |
| `../../LocalTime` | J8 | 9–9 |
| `FormDetailPage` | J8 | 14–130 |

### `src/app/staff/forms/actions.ts`

| symbol | journeys | lines (at generation — stale hint) |
|---|---|---|
| `issueFormLink` | J2 J8 | 15–71 |

### `src/app/staff/page.tsx`

| symbol | journeys | lines (at generation — stale hint) |
|---|---|---|
| `./AddPasskeyButton` | J4 | 7–7 |
| `./LocalTime` | J4 | 8–8 |
| `StaffIndexPage` | J4 | 10–107 |

### `src/components/FormRenderer.tsx`

| symbol | journeys | lines (at generation — stale hint) |
|---|---|---|
| `@/lib/form-def` | J1 | 3–3 |
| `@/components/SignatureField` | J1 | 4–4 |
| `FormRendererProps` | J1 | 6–9 |
| `FormRenderer` | J1 | 11–109 |

### `src/components/SignatureField.tsx`

| symbol | journeys | lines (at generation — stale hint) |
|---|---|---|
| `react` | J1 | 3–3 |
| `signature_pad` | J1 | 4–4 |
| `SignatureFieldProps` | J1 | 6–9 |
| `SignatureField` | J1 | 11–171 |
| `handleResize` | J1 | 60–70 |

### `src/db/index.ts`

| symbol | journeys | lines (at generation — stale hint) |
|---|---|---|
| `drizzle-orm/postgres-js` | J1 J2 J3 J4 J5 J6 J7 J8 | 1–1 |
| `postgres` | J1 J2 J3 J4 J5 J6 J7 J8 | 2–2 |
| `./schema` | J1 J2 J3 J4 J5 J6 J7 J8 | 3–3 |
| `db` | J1 J2 J3 J4 J5 J6 J7 J8 | 17–17 |

### `src/db/schema.ts`

| symbol | journeys | lines (at generation — stale hint) |
|---|---|---|
| `drizzle-orm/pg-core` | J1 J2 J3 J4 J5 J8 | 1–12 |
| `orgMemberRoleEnum` | J1 J2 J3 J4 J5 J8 | 16–16 |
| `submissionStatusEnum` | J1 J2 J3 J4 J5 J8 | 18–23 |
| `scanStatusEnum` | J1 J2 J3 J4 J5 J8 | 25–29 |
| `auditEventTypeEnum` | J1 J2 J3 J4 J5 J8 | 31–37 |
| `paymentUpdateStatusEnum` | J1 J2 J3 J4 J5 J8 | 39–43 |
| `emailDeliveryStatusEnum` | J1 J2 J3 J4 J5 J8 | 45–49 |
| `orgs` | J1 | 53–58 |
| `authenticators` | J4 | 118–133 |
| `orgMembers` | J2 J3 J4 J5 J8 | 135–147 |
| `contacts` | J2 J8 | 149–161 |
| `forms` | J1 J2 J3 J4 J8 | 163–179 |
| `formLinks` | J1 J2 J3 J8 | 181–199 |
| `submissions` | J1 J4 | 201–224 |
| `submissionFiles` | J1 J4 | 226–238 |
| `auditEvents` | J1 J4 | 240–253 |
| `paymentUpdateRequests` | J4 | 255–276 |
| `emailDeliveries` | J1 | 278–289 |

### `src/lib/answers.ts`

| symbol | journeys | lines (at generation — stale hint) |
|---|---|---|
| `./crypto` | J1 J4 | 6–6 |
| `resolveStoredAnswer` | J1 J4 | 27–45 |

### `src/lib/base-url.ts`

| symbol | journeys | lines (at generation — stale hint) |
|---|---|---|
| `getBaseUrl` | J1 J4 J5 J8 | 5–14 |

### `src/lib/crypto.ts`

| symbol | journeys | lines (at generation — stale hint) |
|---|---|---|
| `crypto` | J1 J4 | 1–1 |
| `getKey` | J1 J4 | 7–20 |
| `encryptField` | J1 | 26–42 |
| `decryptField` | J1 J4 | 48–77 |

### `src/lib/email.ts`

| symbol | journeys | lines (at generation — stale hint) |
|---|---|---|
| `DeliveryArgs` | J1 | 5–10 |
| `DeliveryResult` | J1 | 12–16 |
| `sendDelivery` | J1 | 18–65 |

### `src/lib/form-def.ts`

| symbol | journeys | lines (at generation — stale hint) |
|---|---|---|
| `zod` | J1 J3 J4 | 1–1 |
| `yaml` | J1 J3 J4 | 2–2 |
| `FormDef` | J1 J3 J4 | 54–54 |
| `parseFormDef` | J3 | 60–82 |

### `src/lib/limits.ts`

| symbol | journeys | lines (at generation — stale hint) |
|---|---|---|
| `MAX_FILE_BYTES` | J1 | 25–25 |

### `src/lib/payments/dev.ts`

| symbol | journeys | lines (at generation — stale hint) |
|---|---|---|
| `./types` | J4 J5 J6 | 1–1 |
| `devProvider` | J4 J5 J6 | 3–12 |

### `src/lib/payments/index.ts`

| symbol | journeys | lines (at generation — stale hint) |
|---|---|---|
| `./dev` | J4 J5 J6 | 1–1 |
| `./stripe` | J4 J5 J6 | 2–2 |
| `./types` | J4 J5 J6 | 3–3 |
| `getPaymentProvider` | J4 J5 | 5–7 |
| `isStripeActive` | J6 | 9–11 |

### `src/lib/payments/stripe.ts`

| symbol | journeys | lines (at generation — stale hint) |
|---|---|---|
| `stripe` | J4 J5 J6 | 1–1 |
| `./types` | J4 J5 J6 | 2–2 |
| `getStripeClient` | J6 | 4–12 |
| `stripeProvider` | J4 J5 J6 | 14–32 |
| `constructStripeEvent` | J6 | 42–54 |

### `src/lib/payments/types.ts`

| symbol | journeys | lines (at generation — stale hint) |
|---|---|---|
| `PaymentProvider` | J4 J5 J6 | 1–8 |

### `src/lib/pdf.tsx`

| symbol | journeys | lines (at generation — stale hint) |
|---|---|---|
| `./form-def` | J1 | 22–22 |
| `./answers` | J1 | 23–23 |
| `SignatureValue` | J1 | 27–30 |
| `TypedSignatureValue` | J1 | 32–35 |
| `SignatureArg` | J1 | 37–37 |
| `PdfArgs` | J1 | 39–50 |
| `DocProps` | J1 | 154–164 |
| `SubmissionDocument` | J1 | 166–224 |
| `renderSubmissionPdf` | J1 | 228–241 |

### `src/lib/rate-limit.ts`

| symbol | journeys | lines (at generation — stale hint) |
|---|---|---|
| `FIFTEEN_MIN` | J1 | 13–13 |
| `MAX_BUCKETS` | J1 | 23–23 |
| `MAX_KEY_LENGTH` | J1 | 28–28 |
| `normalizeKey` | J1 | 30–33 |
| `SWEEP_INTERVAL_MS` | J1 | 38–38 |
| `lastSweptAt` | J1 | 39–39 |
| `makeRoom` | J1 | 41–59 |
| `rateLimit` | J1 | 65–87 |
| `rateLimitAll` | J1 | 100–109 |

### `src/lib/request-context.ts`

| symbol | journeys | lines (at generation — stale hint) |
|---|---|---|
| `getClientIp` | J1 | 16–22 |
| `getRequestContext` | J1 | 29–37 |

### `src/lib/scan.ts`

| symbol | journeys | lines (at generation — stale hint) |
|---|---|---|
| `EICAR` | J1 | 14–15 |
| `CHUNK_SIZE` | J1 | 17–17 |
| `ScanUnavailableError` | J1 | 20–25 |
| `clamdConfig` | J1 | 27–35 |
| `scanWithClamd` | J1 | 42–93 |
| `scanBytes` | J1 | 99–109 |

### `src/lib/seal.ts`

| symbol | journeys | lines (at generation — stale hint) |
|---|---|---|
| `crypto` | J1 | 1–1 |
| `contentSha256` | J1 | 19–22 |
| `stableStringify` | J1 | 34–45 |

### `src/lib/session.ts`

| symbol | journeys | lines (at generation — stale hint) |
|---|---|---|
| `getActiveUser` | J2 J3 J4 J5 J8 | 16–33 |
| `getActiveUser` | J2 J3 J4 J5 J8 | 16–33 |
| `getActiveOrgId` | J2 J3 J4 J5 J8 | 41–45 |

### `src/lib/storage.ts`

| symbol | journeys | lines (at generation — stale hint) |
|---|---|---|
| `QUARANTINE_PREFIX` | J1 | 12–12 |
| `CLEAN_PREFIX` | J1 | 13–13 |
| `assertSafeKey` | J1 | 18–28 |
| `r2Config` | J1 | 31–40 |
| `usingR2` | J1 | 42–42 |
| `r2Client` | J1 | 44–51 |
| `r2Url` | J1 | 53–57 |
| `r2Put` | J1 | 59–66 |
| `r2Copy` | J1 | 76–84 |
| `r2Delete` | J1 | 86–92 |
| `STORAGE_ROOT` | J1 | 95–95 |
| `QUARANTINE_DIR` | J1 | 96–96 |
| `CLEAN_DIR` | J1 | 97–97 |
| `ensureDirs` | J1 | 99–102 |
| `assertWithin` | J1 | 104–108 |
| `putQuarantine` | J1 | 116–127 |
| `promoteToClean` | J1 | 133–148 |

### `src/lib/tokens.ts`

| symbol | journeys | lines (at generation — stale hint) |
|---|---|---|
| `newLinkToken` | J2 J3 J8 | 7–9 |
| `LINK_TTL_MS` | J2 J3 J8 | 15–15 |

_Entry symbols not verified against source (no parser for the file type), so drift in them cannot be detected: J1 `submitForm` (f/[token]/actions.ts), J1 `Page` (f/[token]/page.tsx), J2 `issueFormLink` (staff/forms/actions.ts), J3 `validateAndPreview` (onboarding/actions.ts), J3 `createFormAndLink` (onboarding/actions.ts), J4 `SubmissionDetailPage` (staff/[submissionId]/page.tsx), J4 `StaffIndexPage` (staff/page.tsx), J5 `createPaymentUpdateRequest` (staff/[submissionId]/payment-actions.ts), J5 `simulatePaymentCompletion` (staff/[submissionId]/payment-actions.ts), J6 `POST` (api/webhooks/stripe/route.ts), J7 `GET` (api/payment-requests/[requestId]/status/route.ts), J8 `FormDetailPage` (staff/forms/[formId]/page.tsx)._

_169 symbols across 38 files reach at least one journey. Symbols reaching none are omitted._
