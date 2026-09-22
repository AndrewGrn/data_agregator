import type * as React from "react";

import { cn } from "../../lib/utils";

export function DataRow({
  leading,
  primary,
  secondary,
  meta,
  actions,
  onClick
}: {
  leading?: React.ReactNode;
  primary: React.ReactNode;
  secondary?: React.ReactNode;
  meta?: React.ReactNode;
  actions?: React.ReactNode;
  onClick?: () => void;
}) {
  return (
    <div
      role={onClick ? "button" : undefined}
      tabIndex={onClick ? 0 : undefined}
      onClick={onClick}
      onKeyDown={
        onClick
          ? (event) => {
              if (event.key === "Enter" || event.key === " ") {
                event.preventDefault();
                onClick();
              }
            }
          : undefined
      }
      className={cn(
        "flex h-12 min-w-0 items-center gap-3 border-b border-border px-3 last:border-b-0",
        onClick && "cursor-pointer transition-colors hover:bg-muted/60 focus-visible:bg-muted/60 focus-visible:outline-none"
      )}
    >
      {leading ? <div className="flex shrink-0 items-center justify-center">{leading}</div> : null}
      <div className="flex min-w-0 flex-1 items-baseline gap-2 overflow-hidden">
        <span className="truncate text-sm font-medium text-foreground">{primary}</span>
        {secondary ? <span className="truncate text-xs text-muted-foreground">{secondary}</span> : null}
      </div>
      {meta ? <div className="flex shrink-0 items-center gap-2">{meta}</div> : null}
      {actions ? (
        <div className="flex shrink-0 items-center gap-1" onClick={(event) => event.stopPropagation()}>
          {actions}
        </div>
      ) : null}
    </div>
  );
}
