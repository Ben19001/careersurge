import type { Pay } from "@/lib/api";

export function formatPay(pay: Pay | null): string | null {
  if (!pay) return null;
  const money = new Intl.NumberFormat("en-US", {
    style: "currency",
    currency: pay.currency || "USD",
    maximumFractionDigits: 0,
  });
  if (pay.min != null && pay.max != null && pay.min !== pay.max) {
    return `${money.format(pay.min)} - ${money.format(pay.max)}`;
  }
  return money.format((pay.max ?? pay.min) as number);
}

// 47 -> "0:47", 3725 -> "1:02:05"
export function formatCountdown(seconds: number): string {
  const total = Math.max(0, Math.round(seconds));
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const secs = String(total % 60).padStart(2, "0");
  return hours > 0 ? `${hours}:${String(minutes).padStart(2, "0")}:${secs}` : `${minutes}:${secs}`;
}

export function formatDuration(seconds: number): string {
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return `${Math.max(minutes, 1)} min`;
  const hours = Math.floor(minutes / 60);
  return `${hours} h ${minutes % 60} min`;
}

export function formatDate(iso: string | null): string {
  if (!iso) return "an unknown date";
  return new Date(iso).toLocaleDateString("en-US", {
    year: "numeric",
    month: "short",
    day: "numeric",
  });
}

export const ATS_LABEL: Record<string, string> = {
  greenhouse: "Greenhouse",
  lever: "Lever",
  ashby: "Ashby",
};
