// The resume rule the server enforces too (backend: RESUME_MAX_BYTES and RESUME_RULE in local_profile.py).
const RESUME_MAX_BYTES = 1024 * 1024;
export const RESUME_RULE = "Your resume must be a PDF under 1 MB.";

// The reason a chosen file is refused before it is sent, or null when it fits the rule.
export function resumeProblem(file: File): string | null {
  const isPdf = file.type === "application/pdf" || file.name.toLowerCase().endsWith(".pdf");
  return isPdf && file.size <= RESUME_MAX_BYTES ? null : RESUME_RULE;
}
