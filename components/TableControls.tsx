"use client";

/**
 * URL-driven search / sort / filter bar.
 *
 * Architecture: the app is server-rendered (RSC). This is the one place we opt
 * into a client component, and even here we do NOT hold data — we only mutate
 * the URL's searchParams. The server component re-reads those params, re-queries
 * / re-orders, and streams fresh HTML (a soft navigation, no full reload). That
 * keeps a single source of truth (the URL), makes every view shareable/
 * bookmarkable, and avoids shipping the dataset to the browser.
 *
 * Reused across /housing, /leads, /risk so every list page gets the same
 * affordances with consistent param names.
 */

import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { useCallback, useEffect, useRef, useState } from "react";

export interface SelectOption {
  value: string;
  label: string;
}

export interface FilterDef {
  /** URL param name, e.g. "tier". */
  param: string;
  label: string;
  /** The empty/"all" option is prepended automatically. */
  options: SelectOption[];
  allLabel?: string;
}

export interface TableControlsProps {
  /** Free-text search. Debounced; writes ?{<param>}=. */
  search?: { param: string; placeholder?: string };
  /** Sort key selector. */
  sort?: { param: string; options: SelectOption[]; defaultValue?: string };
  /** Direction toggle (asc/desc). Defaults param name "dir", default "desc". */
  direction?: { param: string; defaultValue?: "asc" | "desc" };
  /** Zero or more dropdown filters. */
  filters?: FilterDef[];
  /** "Showing X of Y" counter (optional). */
  shown?: number;
  total?: number;
}

const inputCls =
  "h-9 rounded-md border border-zinc-300 dark:border-zinc-700 bg-white " +
  "dark:bg-zinc-900 px-3 text-sm text-zinc-900 dark:text-zinc-100 " +
  "focus:outline-none focus:ring-2 focus:ring-zinc-400 dark:focus:ring-zinc-600";

export function TableControls({
  search,
  sort,
  direction,
  filters = [],
  shown,
  total,
}: TableControlsProps) {
  const router = useRouter();
  const pathname = usePathname();
  const params = useSearchParams();

  const dirParam = direction?.param ?? "dir";
  const dirDefault = direction?.defaultValue ?? "desc";

  // Push a new query string for one changed key. Empty/undefined ⇒ remove it.
  const setParam = useCallback(
    (key: string, value: string | undefined) => {
      const next = new URLSearchParams(params.toString());
      if (value == null || value === "") next.delete(key);
      else next.set(key, value);
      const qs = next.toString();
      router.replace(qs ? `${pathname}?${qs}` : pathname, { scroll: false });
    },
    [params, pathname, router],
  );

  // --- Debounced search input (local state so typing stays responsive) ------
  const [text, setText] = useState(
    search ? (params.get(search.param) ?? "") : "",
  );
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);
  useEffect(() => {
    // Keep local input in sync when the URL changes elsewhere (e.g. Clear).
    if (search) setText(params.get(search.param) ?? "");
  }, [params, search]);

  const onText = (v: string) => {
    setText(v);
    if (!search) return;
    if (timer.current) clearTimeout(timer.current);
    timer.current = setTimeout(() => setParam(search.param, v.trim()), 250);
  };

  const curSort = sort ? (params.get(sort.param) ?? sort.defaultValue ?? "") : "";
  const curDir = params.get(dirParam) ?? dirDefault;

  const hasActive =
    (search && (params.get(search.param) ?? "") !== "") ||
    (sort && params.get(sort.param)) ||
    params.get(dirParam) ||
    filters.some((f) => params.get(f.param));

  // Clear only the params THIS bar owns, preserving unrelated ones (e.g.
  // /risk's cycle/scope/limit) so clearing filters doesn't reset the view.
  const clearAll = () => {
    const next = new URLSearchParams(params.toString());
    if (search) next.delete(search.param);
    if (sort) next.delete(sort.param);
    next.delete(dirParam);
    for (const f of filters) next.delete(f.param);
    const qs = next.toString();
    router.replace(qs ? `${pathname}?${qs}` : pathname, { scroll: false });
  };

  return (
    <div className="flex flex-wrap items-end gap-3">
      {search && (
        <label className="flex flex-col gap-1">
          <span className="text-[10px] font-semibold uppercase tracking-wider text-zinc-500">
            Search
          </span>
          <input
            type="search"
            inputMode="search"
            value={text}
            onChange={(e) => onText(e.target.value)}
            placeholder={search.placeholder ?? "Search…"}
            className={`${inputCls} w-56`}
          />
        </label>
      )}

      {filters.map((f) => (
        <label key={f.param} className="flex flex-col gap-1">
          <span className="text-[10px] font-semibold uppercase tracking-wider text-zinc-500">
            {f.label}
          </span>
          <select
            value={params.get(f.param) ?? ""}
            onChange={(e) => setParam(f.param, e.target.value)}
            className={inputCls}
          >
            <option value="">{f.allLabel ?? "All"}</option>
            {f.options.map((o) => (
              <option key={o.value} value={o.value}>
                {o.label}
              </option>
            ))}
          </select>
        </label>
      ))}

      {sort && (
        <label className="flex flex-col gap-1">
          <span className="text-[10px] font-semibold uppercase tracking-wider text-zinc-500">
            Sort by
          </span>
          <div className="flex items-center gap-1">
            <select
              value={curSort}
              onChange={(e) => setParam(sort.param, e.target.value)}
              className={inputCls}
            >
              {sort.options.map((o) => (
                <option key={o.value} value={o.value}>
                  {o.label}
                </option>
              ))}
            </select>
            <button
              type="button"
              aria-label={`Sort ${curDir === "asc" ? "ascending" : "descending"}`}
              title={curDir === "asc" ? "Ascending" : "Descending"}
              onClick={() =>
                setParam(dirParam, curDir === "asc" ? "desc" : "asc")
              }
              className="h-9 w-9 rounded-md border border-zinc-300 dark:border-zinc-700 bg-white dark:bg-zinc-900 text-sm font-mono hover:bg-zinc-50 dark:hover:bg-zinc-800"
            >
              {curDir === "asc" ? "↑" : "↓"}
            </button>
          </div>
        </label>
      )}

      {hasActive && (
        <button
          type="button"
          onClick={clearAll}
          className="h-9 rounded-md px-3 text-sm text-zinc-600 dark:text-zinc-400 hover:text-zinc-900 dark:hover:text-zinc-100 underline underline-offset-2"
        >
          Clear
        </button>
      )}

      {total != null && (
        <span className="ml-auto self-center text-xs text-zinc-500">
          {shown != null && shown !== total ? (
            <>
              <span className="font-semibold text-zinc-700 dark:text-zinc-300">
                {shown.toLocaleString()}
              </span>{" "}
              of {total.toLocaleString()}
            </>
          ) : (
            <>{total.toLocaleString()} total</>
          )}
        </span>
      )}
    </div>
  );
}
