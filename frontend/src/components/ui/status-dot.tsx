import { cn } from "../../lib/utils";

const toneClasses: Record<"ok" | "warn" | "bad" | "muted", string> = {
  ok: "bg-emerald-500",
  warn: "bg-amber-500",
  bad: "bg-red-500",
  muted: "bg-gray-300"
};

export function StatusDot({ tone, label }: { tone: "ok" | "warn" | "bad" | "muted"; label?: string }) {
  return (
    <span className="inline-flex items-center gap-1.5">
      <span className={cn("inline-block size-2 shrink-0 rounded-full", toneClasses[tone])} aria-hidden="true" />
      {label ? <span className="text-xs text-muted-foreground">{label}</span> : null}
    </span>
  );
}
