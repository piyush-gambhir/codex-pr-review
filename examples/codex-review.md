# Review guidelines

These rules are passed to Codex with every review. Keep them short and concrete;
Codex already looks for bugs, so focus on what is specific to this repository.

## Severity

- Treat any database query that isn't scoped to the caller's tenant as P0.
- Treat personal data (emails, phone numbers, request bodies) in logs or error messages as P1.
- Money is stored as integer cents: any path that can produce fractional cents, NaN or Infinity is P1.

## Always check

- New API routes validate their input and have a test.
- Database migrations are backward compatible with the currently deployed code.

## Ignore

- Generated code under `src/gen/` and lockfiles.
- Formatting and lint issues; CI enforces those.
