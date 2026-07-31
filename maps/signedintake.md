# Journey map — which user journeys depend on which symbols

Target: `/home/ericm/personal_projects/signedintake/main` · index schema 8 · generated from commit `0305ada`

Look up the symbols you changed **by name**. Every journey listed for them may have changed behavior and is worth verifying. This is **recall-first**: a shared symbol legitimately fans out to many journeys.

**Line numbers are frozen at the commit above and are a hint only** — your own edit has already shifted them, so an insertion higher up the file makes the ranges point at the wrong symbol (issue #24). Match the symbol name first, and fall back to the range when you cannot: import nodes and module-level bindings get rows too, and an edit to one of those looks like no symbol you touched. An edit you cannot attribute to any row is *unknown*, never *no journeys*.

`!` marks a journey reached only through weak or synthesized graph edges — treat it as *verify manually*, not as *probably fine*.

## Journeys

- **J1** claimant submits a signed form — entry: `submitForm`, `Page`
- **J2** staff issues a form link — entry: `issueFormLink`
- **J3** onboarding: form created from YAML — entry: `validateAndPreview`, `createFormAndLink`, `OnboardingPage`
- **J4** staff reviews a submission — entry: `SubmissionDetailPage`, `StaffIndexPage`
- **J5** staff requests a payment update — entry: `createPaymentUpdateRequest`, `simulatePaymentCompletion`
- **J6** stripe webhook settles a payment — entry: `POST`
- **J7** client polls payment status — entry: `GET`
- **J8** staff browses forms — entry: `FormsIndexPage`, `FormDetailPage`
- **J9** staff signs in — entry: `LoginPage`
- **J10** staff regenerates and downloads the signed PDF — entry: `regenerateSubmissionPdf`, `GET`
- **J11** customer completes a dev-provider payment — entry: `DevSimulatePage`, `simulatePaymentCompletion`
- **J12** visitor lands on the marketing home page — entry: `Home`
- **J13** server boots with a safe production config — entry: `register`
- **J14** operator seeds a staff account from the CLI — entry: `main`, `ensureDemoOrg`

## Symbols by file

### `scripts/seed-staff.ts`

| symbol | journeys | lines (at generation — stale hint) |
|---|---|---|
| `../src/db` | J14 | 12–12 |
| `../src/db/schema` | J14 | 13–13 |
| `../src/lib/seed` | J14 | 14–14 |
| `main` | J14 | 17–62 |

### `src/app/api/payment-requests/[requestId]/status/route.ts`

| symbol | journeys | lines (at generation — stale hint) |
|---|---|---|
| `GET` | J7 | 8–28 |

### `src/app/api/webhooks/stripe/route.ts`

| symbol | journeys | lines (at generation — stale hint) |
|---|---|---|
| `POST` | J6 | 13–94 |

### `src/app/f/[token]/FillForm.tsx`

| symbol | journeys | lines (at generation — stale hint) |
|---|---|---|
| `./actions` | J1 J4 J10 | 4–4 |
| `FillFormProps` | J1 J4 J10 | 8–14 |
| `FillForm` | J1 J4 J10 | 18–96 |

### `src/app/f/[token]/actions.ts`

| symbol | journeys | lines (at generation — stale hint) |
|---|---|---|
| `LinkAlreadyUsedError` | J1 J4 J10 | 32–32 |
| `SubmitResult` | J1 J4 J10 | 34–34 |
| `submitForm` | J1 J4 J10 | 36–397 |

### `src/app/f/[token]/page.tsx`

| symbol | journeys | lines (at generation — stale hint) |
|---|---|---|
| `./FillForm` | J1 J4 J10 | 8–8 |
| `Page` | J1 J4 J10 | 10–114 |

### `src/app/login/PasskeyButton.tsx`

| symbol | journeys | lines (at generation — stale hint) |
|---|---|---|
| `PasskeyButton` | J9 | 11–45 |

### `src/app/login/page.tsx`

| symbol | journeys | lines (at generation — stale hint) |
|---|---|---|
| `./PasskeyButton` | J9 | 5–5 |
| `LoginPage` | J9 | 11–112 |

### `src/app/onboarding/OnboardingEditor.tsx`

| symbol | journeys | lines (at generation — stale hint) |
|---|---|---|
| `./actions` | J3 | 4–4 |
| `SAMPLE_YAML` | J3 | 8–51 |
| `OnboardingEditor` | J3 | 65–179 |

### `src/app/onboarding/actions.ts`

| symbol | journeys | lines (at generation — stale hint) |
|---|---|---|
| `validateAndPreview` | J3 | 9–29 |
| `createFormAndLink` | J3 | 31–78 |

### `src/app/onboarding/page.tsx`

| symbol | journeys | lines (at generation — stale hint) |
|---|---|---|
| `./OnboardingEditor` | J3 | 3–3 |
| `OnboardingPage` | J3 | 7–11 |

### `src/app/page.tsx`

| symbol | journeys | lines (at generation — stale hint) |
|---|---|---|
| `Home` | J12 | 3–21 |

### `src/app/pay/[requestId]/SimulateButton.tsx`

| symbol | journeys | lines (at generation — stale hint) |
|---|---|---|
| `Props` | J11 | 6–9 |
| `SimulateButton` | J11 | 11–56 |

### `src/app/pay/[requestId]/page.tsx`

| symbol | journeys | lines (at generation — stale hint) |
|---|---|---|
| `./SimulateButton` | J11 | 5–5 |
| `DevSimulatePage` | J11 | 10–64 |

### `src/app/staff/AddPasskeyButton.tsx`

| symbol | journeys | lines (at generation — stale hint) |
|---|---|---|
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
| `Props` | J4 | 16–24 |
| `StatusBadge` | J4! | 26–38 |
| `LiveStatusBadge` | J4! | 40–74 |
| `PaymentSection` | J4 | 76–142 |

### `src/app/staff/[submissionId]/RegeneratePdfButton.tsx`

| symbol | journeys | lines (at generation — stale hint) |
|---|---|---|
| `./pdf-actions` | J4 | 5–5 |
| `RegeneratePdfButton` | J4 | 10–42 |

### `src/app/staff/[submissionId]/page.tsx`

| symbol | journeys | lines (at generation — stale hint) |
|---|---|---|
| `./PaymentSection` | J4 | 10–10 |
| `./RegeneratePdfButton` | J4 | 11–11 |
| `../LocalTime` | J4 | 12–12 |
| `SubmissionDetailPage` | J4 | 15–266 |

### `src/app/staff/[submissionId]/payment-actions.ts`

| symbol | journeys | lines (at generation — stale hint) |
|---|---|---|
| `createPaymentUpdateRequest` | J4 J5 | 11–84 |
| `simulatePaymentCompletion` | J5 J11 | 86–122 |

### `src/app/staff/[submissionId]/pdf-actions.ts`

| symbol | journeys | lines (at generation — stale hint) |
|---|---|---|
| `RegenerateResult` | J4 J10 | 16–16 |
| `ALREADY_SEALED` | J4 J10 | 20–20 |
| `regenerateSubmissionPdf` | J4 J10 | 36–138 |

### `src/app/staff/[submissionId]/pdf/route.ts`

| symbol | journeys | lines (at generation — stale hint) |
|---|---|---|
| `GET` | J10 | 12–69 |

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
| `issueFormLink` | J2 J8 | 16–88 |

### `src/app/staff/forms/page.tsx`

| symbol | journeys | lines (at generation — stale hint) |
|---|---|---|
| `FormsIndexPage` | J8 | 9–71 |

### `src/app/staff/page.tsx`

| symbol | journeys | lines (at generation — stale hint) |
|---|---|---|
| `./AddPasskeyButton` | J4 | 7–7 |
| `./LocalTime` | J4 | 8–8 |
| `StaffIndexPage` | J4 | 11–108 |

### `src/components/FormRenderer.tsx`

| symbol | journeys | lines (at generation — stale hint) |
|---|---|---|
| `FormRendererProps` | J1 J3 J4 J10 | 6–12 |
| `FormRenderer` | J1 J3 J4 J10 | 14–117 |

### `src/components/SignatureField.tsx`

| symbol | journeys | lines (at generation — stale hint) |
|---|---|---|
| `SignatureFieldProps` | J1 J3 J4 J10 | 6–9 |
| `SignatureField` | J1 J3 J4 J10 | 11–171 |
| `handleResize` | J1 J3 J4 J10 | 60–70 |

### `src/db/index.ts`

| symbol | journeys | lines (at generation — stale hint) |
|---|---|---|
| `./schema` | J1 J2 J3 J4 J5 J6 J7 J8 J10 J11 J14 | 3–3 |
| `db` | J1 J2 J3 J4 J5 J6 J7 J8 J10 J11 J14 | 17–17 |

### `src/db/schema.ts`

| symbol | journeys | lines (at generation — stale hint) |
|---|---|---|
| `orgMemberRoleEnum` | J1 J2 J3 J4 J5 J6 J7 J8 J10 J11 J14 | 16–16 |
| `submissionStatusEnum` | J1 J2 J3 J4 J5 J6 J7 J8 J10 J11 J14 | 18–23 |
| `scanStatusEnum` | J1 J2 J3 J4 J5 J6 J7 J8 J10 J11 J14 | 25–29 |
| `auditEventTypeEnum` | J1 J2 J3 J4 J5 J6 J7 J8 J10 J11 J14 | 31–45 |
| `paymentUpdateStatusEnum` | J1 J2 J3 J4 J5 J6 J7 J8 J10 J11 J14 | 47–51 |
| `emailDeliveryStatusEnum` | J1 J2 J3 J4 J5 J6 J7 J8 J10 J11 J14 | 53–57 |
| `orgs` | J1 J4 J10 J14 | 61–66 |
| `users` | J4 J14 | 68–78 |
| `authenticators` | J4 | 126–141 |
| `orgMembers` | J2 J3 J4 J5 J8 J10 J11 J14 | 143–155 |
| `contacts` | J2 J8 | 157–169 |
| `forms` | J1 J2 J3 J4 J8 J10 | 171–187 |
| `formLinks` | J1 J2 J3 J4 J5 J8 J10 J11 | 189–207 |
| `submissions` | J1 J4 J5 J10 J11 | 209–232 |
| `submissionFiles` | J1 J4 J10 | 234–246 |
| `auditEvents` | J1 J2 J4 J5 J8 J10 J11 | 248–265 |
| `paymentUpdateRequests` | J4 J5 J6 J7 J11 | 267–288 |
| `emailDeliveries` | J1 J4 J10 | 290–301 |

### `src/instrumentation.ts`

| symbol | journeys | lines (at generation — stale hint) |
|---|---|---|
| `register` | J13 | 19–119 |

### `src/lib/answers.ts`

| symbol | journeys | lines (at generation — stale hint) |
|---|---|---|
| `./crypto` | J1 J4 J10 | 6–6 |
| `resolveStoredAnswer` | J1 J4 J10 | 27–45 |

### `src/lib/audit.ts`

| symbol | journeys | lines (at generation — stale hint) |
|---|---|---|
| `StaffAuditEventType` | J2 J4 J5 J8 J10 J11 | 9–14 |
| `recordStaffAuditEvent` | J2 J4 J5 J8 J10 J11 | 22–40 |

### `src/lib/base-url.ts`

| symbol | journeys | lines (at generation — stale hint) |
|---|---|---|
| `getBaseUrl` | J1 J4 J5 J8 J10 J11 | 5–14 |

### `src/lib/crypto.ts`

| symbol | journeys | lines (at generation — stale hint) |
|---|---|---|
| `ALGORITHM` | J1 J4 J10 | 3–3 |
| `IV_BYTES` | J1 J4 J10 | 4–4 |
| `TAG_BYTES` | J1 J4 J10 | 5–5 |
| `getKey` | J1 J4 J10 | 7–20 |
| `encryptField` | J1 J4 J10 | 26–42 |
| `decryptField` | J1 J4 J10 | 48–77 |

### `src/lib/email.ts`

| symbol | journeys | lines (at generation — stale hint) |
|---|---|---|
| `DeliveryArgs` | J1 J4 J10 | 5–10 |
| `DeliveryResult` | J1 J4 J10 | 12–16 |
| `sendDelivery` | J1 J4 J10 | 18–65 |

### `src/lib/events.ts`

| symbol | journeys | lines (at generation — stale hint) |
|---|---|---|
| `IntakeEventType` | J1 J4 J10 | 9–12 |
| `EventInput` | J1 J4 J10 | 14–21 |
| `SignedEvent` | J1 J4 J10 | 24–32 |
| `canonical` | J1 J4 J10 | 38–47 |
| `buildSignedEvent` | J1 J4 J10 | 49–60 |
| `emitEvent` | J1 J4 J10 | 66–95 |

### `src/lib/form-def.ts`

| symbol | journeys | lines (at generation — stale hint) |
|---|---|---|
| `fieldTypeEnum` | J3 | 5–13 |
| `fieldSchema` | J3 | 15–36 |
| `formDefSchema` | J3 | 38–52 |
| `FormDef` | J1 J3 J4 J10 | 54–54 |
| `parseFormDef` | J3 | 60–82 |

### `src/lib/labels.ts`

| symbol | journeys | lines (at generation — stale hint) |
|---|---|---|
| `LABEL_OVERRIDES` | J4 | 12–15 |
| `humanizeEnum` | J4 | 17–23 |

### `src/lib/limits.ts`

| symbol | journeys | lines (at generation — stale hint) |
|---|---|---|
| `MAX_FILE_BYTES` | J1 J4 J10 | 25–25 |

### `src/lib/payments/dev.ts`

| symbol | journeys | lines (at generation — stale hint) |
|---|---|---|
| `./types` | J4 J5 J6 J11 | 1–1 |
| `devProvider` | J4 J5 J6 J11 | 3–12 |

### `src/lib/payments/index.ts`

| symbol | journeys | lines (at generation — stale hint) |
|---|---|---|
| `./dev` | J4 J5 J6 J11 | 1–1 |
| `./stripe` | J4 J5 J6 J11 | 2–2 |
| `./types` | J4 J5 J6 J11 | 3–3 |
| `getPaymentProvider` | J4 J5 J11 | 5–7 |
| `isStripeActive` | J6 | 9–11 |

### `src/lib/payments/stripe.ts`

| symbol | journeys | lines (at generation — stale hint) |
|---|---|---|
| `./types` | J4 J5 J6 J11 | 2–2 |
| `getStripeClient` | J6 | 4–12 |
| `stripeProvider` | J4 J5 J6 J11 | 14–32 |
| `constructStripeEvent` | J6 | 42–54 |

### `src/lib/payments/types.ts`

| symbol | journeys | lines (at generation — stale hint) |
|---|---|---|
| `PaymentProvider` | J4 J5 J6 J11 | 1–8 |

### `src/lib/pdf.tsx`

| symbol | journeys | lines (at generation — stale hint) |
|---|---|---|
| `./form-def` | J1 J4 J10 | 22–22 |
| `./answers` | J1 J4 J10 | 23–23 |
| `SignatureValue` | J1 J4 J10 | 27–30 |
| `TypedSignatureValue` | J1 J4 J10 | 32–35 |
| `SignatureArg` | J1 J4 J10 | 37–37 |
| `PdfArgs` | J1 J4 J10 | 39–50 |
| `DocProps` | J1 J4 J10 | 154–164 |
| `SubmissionDocument` | J1 J4 J10 | 166–224 |
| `renderSubmissionPdf` | J1 J4 J10 | 228–241 |

### `src/lib/prefill.ts`

| symbol | journeys | lines (at generation — stale hint) |
|---|---|---|
| `PREFILLABLE_TYPES` | J1 J4 J10 | 6–6 |
| `computePrefill` | J1 J4 J10 | 24–40 |

### `src/lib/rate-limit.ts`

| symbol | journeys | lines (at generation — stale hint) |
|---|---|---|
| `FIFTEEN_MIN` | J1 J4 J9 J10 | 13–13 |
| `MAX_BUCKETS` | J1 J4 J9 J10 | 23–23 |
| `MAX_KEY_LENGTH` | J1 J4 J9 J10 | 28–28 |
| `normalizeKey` | J1 J4 J9 J10 | 30–33 |
| `SWEEP_INTERVAL_MS` | J1 J4 J9 J10 | 38–38 |
| `lastSweptAt` | J1 J4 J9 J10 | 39–39 |
| `makeRoom` | J1 J4 J9 J10 | 41–59 |
| `rateLimit` | J1 J4 J9 J10 | 65–87 |
| `rateLimitAll` | J1 J4 J9 J10 | 100–109 |

### `src/lib/request-context.ts`

| symbol | journeys | lines (at generation — stale hint) |
|---|---|---|
| `getClientIp` | J1 J4 J9 J10 | 16–22 |
| `getRequestContext` | J1 J4 J10 | 29–37 |

### `src/lib/scan.ts`

| symbol | journeys | lines (at generation — stale hint) |
|---|---|---|
| `EICAR` | J1 J4 J10 | 14–15 |
| `CHUNK_SIZE` | J1 J4 J10 | 17–17 |
| `ScanUnavailableError` | J1 J4 J10 | 20–25 |
| `clamdConfig` | J1 J4 J10 | 27–35 |
| `scanWithClamd` | J1 J4 J10 | 42–93 |
| `scanBytes` | J1 J4 J10 | 99–109 |

### `src/lib/seal-submission.ts`

| symbol | journeys | lines (at generation — stale hint) |
|---|---|---|
| `AlreadySealedError` | J1 J4 J10 | 29–34 |
| `SealResult` | J1 J4 J10 | 36–45 |
| `sealSubmissionPdf` | J1 J4 J10 | 47–135 |
| `signatureFromAnswers` | J4 J10 | 147–158 |

### `src/lib/seal.ts`

| symbol | journeys | lines (at generation — stale hint) |
|---|---|---|
| `contentSha256` | J1 J4 J10 | 19–22 |
| `stableStringify` | J1 J4 J10 | 34–45 |

### `src/lib/seed.ts`

| symbol | journeys | lines (at generation — stale hint) |
|---|---|---|
| `DEMO_SLUG` | J14 | 5–5 |
| `DEMO_NAME` | J14 | 6–6 |
| `ensureDemoOrg` | J14 | 11–50 |

### `src/lib/session.ts`

| symbol | journeys | lines (at generation — stale hint) |
|---|---|---|
| `getActiveUser` | J2 J3 J4 J5 J8 J10 J11 | 16–33 |
| `getActiveUser` | J2 J3 J4 J5 J8 J10 J11 | 16–33 |
| `getActiveOrgId` | J2 J3 J4 J5 J8 J10 J11 | 41–45 |

### `src/lib/storage.ts`

| symbol | journeys | lines (at generation — stale hint) |
|---|---|---|
| `QUARANTINE_PREFIX` | J1 J4 J10 | 12–12 |
| `CLEAN_PREFIX` | J1 J4 J10 | 13–13 |
| `assertSafeKey` | J1 J4 J10 | 18–28 |
| `r2Config` | J1 J4 J10 | 31–40 |
| `usingR2` | J1 J4 J10 | 42–42 |
| `r2Client` | J1 J4 J10 | 44–51 |
| `r2Url` | J1 J4 J10 | 53–57 |
| `r2Put` | J1 J4 J10 | 59–66 |
| `r2Get` | J10 | 68–74 |
| `r2Copy` | J1 J4 J10 | 76–84 |
| `r2Delete` | J1 J4 J10 | 86–92 |
| `STORAGE_ROOT` | J1 J4 J10 | 95–95 |
| `QUARANTINE_DIR` | J1 J4 J10 | 96–96 |
| `CLEAN_DIR` | J1 J4 J10 | 97–97 |
| `ensureDirs` | J1 J4 J10 | 99–102 |
| `assertWithin` | J1 J4 J10 | 104–108 |
| `putQuarantine` | J1 J4 J10 | 116–127 |
| `promoteToClean` | J1 J4 J10 | 133–148 |
| `getCleanBytes` | J10 | 155–170 |

### `src/lib/tokens.ts`

| symbol | journeys | lines (at generation — stale hint) |
|---|---|---|
| `newLinkToken` | J2 J3 J8 | 7–9 |
| `LINK_TTL_MS` | J2 J3 J8 | 15–15 |

_Entry symbols not verified against source (no parser for the file type), so drift in them cannot be detected: J1 `submitForm` (src/app/f/[token]/actions.ts), J1 `Page` (src/app/f/[token]/page.tsx), J2 `issueFormLink` (src/app/staff/forms/actions.ts), J3 `validateAndPreview` (src/app/onboarding/actions.ts), J3 `createFormAndLink` (src/app/onboarding/actions.ts), J3 `OnboardingPage` (src/app/onboarding/page.tsx), J4 `SubmissionDetailPage` (src/app/staff/[submissionId]/page.tsx), J4 `StaffIndexPage` (src/app/staff/page.tsx), J5 `createPaymentUpdateRequest` (src/app/staff/[submissionId]/payment-actions.ts), J5 `simulatePaymentCompletion` (src/app/staff/[submissionId]/payment-actions.ts), J6 `POST` (src/app/api/webhooks/stripe/route.ts), J7 `GET` (src/app/api/payment-requests/[requestId]/status/route.ts), J8 `FormsIndexPage` (src/app/staff/forms/page.tsx), J8 `FormDetailPage` (src/app/staff/forms/[formId]/page.tsx), J9 `LoginPage` (src/app/login/page.tsx), J10 `regenerateSubmissionPdf` (src/app/staff/[submissionId]/pdf-actions.ts), J10 `GET` (src/app/staff/[submissionId]/pdf/route.ts), J11 `DevSimulatePage` (src/app/pay/[requestId]/page.tsx), J11 `simulatePaymentCompletion` (src/app/staff/[submissionId]/payment-actions.ts), J12 `Home` (src/app/page.tsx), J13 `register` (src/instrumentation.ts), J14 `main` (scripts/seed-staff.ts), J14 `ensureDemoOrg` (src/lib/seed.ts)._

_190 symbols across 57 files reach at least one journey. Symbols reaching none are omitted._
