# Upstream integration status

Target: `v1.99.1` (`10f4033437`), based on fork `137cafe3d3`

The user authorized completing this merge, pushing it to litellm_internal_staging, and following it through deployment on 2026-09-05. Required commit and deployment checks are in progress

## Resolutions

Both price maps were merged by model key using base/ours/theirs equality. No model entry had conflicting changes on both sides. Local pricing overrides and upstream additions are retained

Gateway discovery paths retain capabilities and add upstream A2A routing. Runtime Docker configuration retains the local cost map and upstream offline Prisma configuration

Provider registration retains BytePlus and Tencent while adopting the upstream builder. Dynamic provider parameters retain tools for models with unknown capabilities and add reasoning_effort for supported models. OpenRouter prefix handling removes only the leading routing prefix

Video serialization retains output_url, last_frame_url and seed. Video cost calculation retains token counts and video-input price differentiation. Verification tokens retain app_id and token_type; spend objects retain capability attribution

Authentication retains token-bound app identity and upstream handling of malformed request bodies. Agent listing/get retain the fork's granted/public/owned visibility. A compatibility method maps the upstream access union to explicit agent grants without interpreting unscoped access as global discovery

Model discovery retains capability flags and adopts upstream cost-map/configured-limit validation. Agent page routing follows the new upstream directory layout with a Marketplace tab; legacy agents.tsx is removed

## Verification and remaining work

The original fork's video billing and capabilities baseline passes 29 tests after installing expression 5.6.0 from the existing lockfile

All non-generated merge conflicts have been resolved. Undefined-name checks across litellm and gateway pass. Expanded merged-branch tests pass: 240 passed, 2 skipped. The temporary log from that checkpoint was not retained; current release regressions are recorded below

All merge conflicts are resolved. The generated output directory was replaced as a whole from the successful final UI build; SHA-256 manifests match for all 888 files

The upstream UI now requires Node >=24.14.1 and npm >=11.10.0 and uses React 19. Dependencies installed with the local Node 26 runtime. Custom Marketplace, App, Skills and by-app usage components now use the current Base UI components and React Query. They retain creation, visibility, publishing, secret rotation, agent import and usage filtering. Component filenames follow upstream conventions. Soniox registration and logo fallbacks were corrected during compilation. The five components pass ESLint with no warnings; 11 interaction regressions pass, including stale-request isolation and invalid date ranges. The final Next.js production build passes. The remaining AVIF notice is an upstream image optimization warning; the asset is emitted unchanged

Prisma schema validation passes. The original-to-candidate schema diff contains additions and no DROP or ALTER COLUMN statements. An isolated local PostgreSQL rehearsal loaded the original schema with synthetic key, app and spend records, applied the candidate schema diff in a transaction, and verified ownership, app attribution, spend and skill attribution. A pre-upgrade pg_dump restored successfully into a separate database with matching records and the original schema. The temporary SQL and rehearsal logs from that checkpoint were not retained

This schema-diff rehearsal does not cover the production migration runner, custom migration-only constraints, production data volume, concurrent traffic or restore timing on a production backup. The release inventory and refreshed production backup are recorded below. Production-runner execution and post-deployment verification remain outstanding. The merge was committed as fdb60502e2 and CI coverage was completed in 188149b890; both were pushed to the default branch


## Release tracking

The current healthy production deployment is b0129d19-7b61-4836-b6ab-91e6cd9e64fb, commit c5b27125bc1e657f85deddaa6419dcf940a8724e. The later deployment of 137cafe3d3 failed during Docker build because python3 resolved to 3.14 while that revision required Python <3.14. The candidate Dockerfile fixes Python to 3.13 and disables interpreter downloads

Railway project e1977026-2eea-4467-8bdb-2b3f6c71d6c4, service fa1aaa7a-eb0b-4b95-9d2b-80993486597a. Production is 9d4409c5-5f77-49a1-a699-98206590d2db; staging is 43342126-1fea-4302-8203-ab0cf3885a21. Production PostgreSQL is 18.6, approximately 611 MB. Public liveness responds successfully before the upgrade

## Final release validation

The locked Python 3.13 environment passes 229 regression tests with 1 skip across BytePlus, Tencent, capabilities, agent access, proxy utilities and importers. Personal budget authentication passes 4 tests. After readonly adapter changes, capabilities, agent permissions and OpenAI-compatible providers pass 202 tests with 6 skips. Model listing coercion and fallback tests pass 21 tests. These suites overlap and must not be added into a unique-test total

The shared dashboard HTTP transport and custom administration flows pass 29 tests. The final production UI build passes and all 888 exported files match their committed deployment directory by SHA-256. Debug-only provider console output was removed. Model listing duplicate token-limit fields were removed and capability token limits now use the upstream integer coercion helper

Upstream strict quality ceilings do not cover all violations already present in the tagged source or the fork. UPSTREAM-QUALITY-BASELINE.json records source revisions and separately measured inherited debt. New adapter collection violations were corrected before reconciliation. No checkers or rules were disabled. The reconciled ceilings are subsequently ratcheted by make lint-budget-update

A refreshed production PostgreSQL backup completed at /Users/javen/.local/state/tokenhub-backups/before-upstream-v1.99.1-20260905-192634.dump (40,265,692 bytes, mode 0600); pg_restore --list succeeds. This is an archive readability check, not a timed full production restore

Budget ratcheting completed: strict Ruff ceilings reduced by 2,267, type-discipline ceilings by 72, test-quality ceilings by 499 and basedpyright ceilings by 16,207 versus the fork branch point. Final make pre-commit passes: Python lint and budgets, e2e type checks, dashboard formatting/lint/budgets and generated API-type synchronization

## Capability discovery follow-up

Production deployment 9bc8f646-e5df-4d1f-a7e4-fa7c543a5e4a reached SUCCESS on commit 188149b890. Liveness and database readiness passed and the same 33 model IDs were retained. A capabilities request exposed a blocking provider-authentication path: supports_* helpers resolve ChatGPT credentials and wait for a device login. The service was restarted and liveness recovered before further discovery requests were attempted

Discovery now reads the existing model-cost capability metadata without calling provider resolution or authentication. The sample_spec documentation entry is excluded from the model catalog. The complete local catalog (3,226 entries) builds in 0.014 seconds; 131 capability and model-listing regressions pass. A guarded before/after check confirms provider initialization was called by the previous implementation and is never called by the fix. Deployment verification of this follow-up is pending
