import type { ReactNode } from "react";

/**
 * Standard page heading: a title, optional one-line description, and an
 * optional right-aligned actions slot (e.g. Add / Import buttons). Used by every
 * top-level page so the header reads consistently now that the SubNav is gone.
 */
export function PageHeader({
  title,
  description,
  actions,
}: {
  title: string;
  description?: string;
  actions?: ReactNode;
}) {
  return (
    <div className="flex flex-wrap items-start justify-between gap-4 pb-5 pt-9">
      <div>
        <h1 className="text-[22px] font-semibold leading-none tracking-[-0.014em] text-foreground">
          {title}
        </h1>
        {description ? (
          <p
            className="mt-2.5 text-[12px] text-muted-foreground"
            style={{ letterSpacing: "-0.003em" }}
          >
            {description}
          </p>
        ) : null}
      </div>
      {actions ? (
        <div className="flex shrink-0 items-center gap-2 pt-0.5">{actions}</div>
      ) : null}
    </div>
  );
}
