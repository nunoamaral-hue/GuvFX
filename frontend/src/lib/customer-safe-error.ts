type KnownCustomerError = {
  match: RegExp;
  message: string;
};

/**
 * Convert a caught API/transport failure into reviewed customer copy.
 *
 * Backend details remain available on the error object for diagnostics, but are
 * never displayed unless a caller explicitly maps the expected condition.
 */
export function customerSafeError(
  error: unknown,
  fallback: string,
  known: KnownCustomerError[] = [],
): string {
  const message = error instanceof Error ? error.message : "";
  for (const rule of known) {
    if (rule.match.test(message)) return rule.message;
  }
  return fallback;
}
