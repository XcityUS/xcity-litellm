import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { z } from "zod";
import dayjs from "dayjs";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { getUsageByApp } from "../networking";

const rowFields = {
  app_id: z.string().nullable(),
  total_spend: z.number(),
  total_requests: z.number(),
  total_tokens: z.number().optional(),
  entity_breakdown: z.record(z.string(), z.object({ spend: z.number(), requests: z.number() })).optional(),
};
const rowSchema = z.object(rowFields);
type Usage = z.infer<typeof rowSchema>;
export default function ByAppUsage({ accessToken }: { accessToken: string }) {
  const [range, setRange] = useState({
    start: dayjs().subtract(7, "day").format("YYYY-MM-DD"),
    end: dayjs().format("YYYY-MM-DD"),
  });
  const [entity, setEntity] = useState("");
  const [page, setPage] = useState(0);
  const [descending, setDescending] = useState(true);
  const [sort, setSort] = useState<"total_spend" | "total_requests">("total_spend");
  const changeRange = (value: typeof range) => {
    setRange(value);
    setPage(0);
  };
  const validRange = Boolean(range.start && range.end && range.start <= range.end);
  const usageQuery = useQuery({
    queryKey: ["xct-app-usage", accessToken, range.start, range.end],
    enabled: Boolean(accessToken) && validRange,
    queryFn: async (): Promise<Usage[]> => {
      const result: unknown = await getUsageByApp(accessToken, {
        start_date: dayjs(range.start).startOf("day").toISOString(),
        end_date: dayjs(range.end).endOf("day").toISOString(),
        limit: 100,
      });
      if (Array.isArray(result)) return z.array(rowSchema).parse(result);
      const envelope = z
        .object({ rows: z.array(rowSchema).optional(), by_app: z.array(rowSchema).optional() })
        .parse(result);
      return z.array(rowSchema).parse(envelope.rows ?? envelope.by_app);
    },
  });
  const rows = usageQuery.data ?? [];
  const loading = usageQuery.isFetching;
  const rangeError = validRange ? "" : "Choose a valid date range.";
  const error = usageQuery.isError ? "App usage is unavailable. Try again later." : rangeError;
  const refresh = () => {
    setPage(0);
    if (validRange) void usageQuery.refetch();
  };
  const changeSort = (column: "total_spend" | "total_requests") => {
    setDescending(sort === column ? !descending : true);
    setSort(column);
    setPage(0);
  };
  const sortArrow = descending ? "↓" : "↑";
  const filtered = rows
    .flatMap((row) => {
      if (!entity) return [row];
      const slice = row.entity_breakdown?.[entity];
      return slice
        ? [{ ...row, total_spend: slice.spend, total_requests: slice.requests, total_tokens: undefined }]
        : [];
    })
    .sort((a, b) => (descending ? 1 : -1) * (b[sort] - a[sort]));
  return (
    <section className="space-y-4 rounded-lg border p-4">
      <h2 className="text-xl font-semibold">Usage by App</h2>
      <div className="flex flex-wrap items-end gap-3">
        {[
          ["Today", 0],
          ["7 days", 7],
          ["30 days", 30],
        ].map(([label, days]) => (
          <Button
            key={label}
            variant="outline"
            onClick={() =>
              changeRange({
                start: dayjs().subtract(Number(days), "day").format("YYYY-MM-DD"),
                end: dayjs().format("YYYY-MM-DD"),
              })
            }
          >
            {label}
          </Button>
        ))}
        <label className="grid gap-1">
          Start date
          <Input
            type="date"
            value={range.start}
            onChange={(event) => changeRange({ ...range, start: event.target.value })}
          />
        </label>
        <label className="grid gap-1">
          End date
          <Input
            type="date"
            value={range.end}
            onChange={(event) => changeRange({ ...range, end: event.target.value })}
          />
        </label>
        <label className="grid gap-1">
          Entity type
          <select
            className="rounded border bg-background p-2"
            value={entity}
            onChange={(event) => {
              setEntity(event.target.value);
              setPage(0);
            }}
          >
            <option value="">All entity types</option>
            {[...new Set(rows.flatMap((row) => Object.keys(row.entity_breakdown ?? {})))].map((type) => (
              <option key={type}>{type}</option>
            ))}
          </select>
        </label>
        <Button disabled={loading} onClick={refresh}>
          Refresh
        </Button>
      </div>
      {error && <p role="alert">{error}</p>}
      {loading && <p role="status">Loading usage…</p>}
      <p>
        Total requests: {filtered.reduce((sum, row) => sum + row.total_requests, 0).toLocaleString()} · Total spend: $
        {filtered.reduce((sum, row) => sum + row.total_spend, 0).toFixed(4)}
      </p>
      <div className="overflow-x-auto">
        <table className="w-full text-left text-sm">
          <thead>
            <tr>
              <th>App</th>
              <th>
                <Button variant="ghost" onClick={() => changeSort("total_requests")}>
                  Requests {sort === "total_requests" ? sortArrow : ""}
                </Button>
              </th>
              <th>Tokens</th>
              <th>
                <Button variant="ghost" onClick={() => changeSort("total_spend")}>
                  Spend (USD) {sort === "total_spend" ? sortArrow : ""}
                </Button>
              </th>
            </tr>
          </thead>
          <tbody>
            {filtered.slice(page * 20, page * 20 + 20).map((row) => (
              <tr className="border-t" key={row.app_id ?? "none"}>
                <td className="p-2">{row.app_id ?? "No app attribution"}</td>
                <td>{row.total_requests.toLocaleString()}</td>
                <td>{row.total_tokens?.toLocaleString() ?? "—"}</td>
                <td>${row.total_spend.toFixed(4)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {!loading && !filtered.length && <p>No activity in this window.</p>}
      <div className="flex gap-3">
        <Button variant="outline" disabled={!page} onClick={() => setPage(page - 1)}>
          Previous
        </Button>
        <span>Page {page + 1}</span>
        <Button variant="outline" disabled={(page + 1) * 20 >= filtered.length} onClick={() => setPage(page + 1)}>
          Next
        </Button>
      </div>
    </section>
  );
}
