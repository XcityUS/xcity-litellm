import { useQuery } from "@tanstack/react-query";
import { useState, type FormEvent } from "react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { Dialog, DialogContent, DialogTitle } from "@/components/ui/dialog";
import { toast } from "@/lib/toast";
import { createXCTApp, deleteXCTApp, listXCTApps, patchXCTApp, rotateXCTAppSecret } from "../networking";

interface AppRow {
  app_id: string;
  app_name: string;
  display_name: string;
  oauth_client_id: string;
  redirect_uris: string[];
  capability_scope_id?: string;
  is_active: boolean;
}
export default function AppTable({ accessToken }: { accessToken: string }) {
  const [busy, setBusy] = useState(false);
  const [createOpen, setCreateOpen] = useState(false);
  const [page, setPage] = useState(0);
  const [reveal, setReveal] = useState<{ secret: string; clientId: string } | null>(null);
  const [showSecret, setShowSecret] = useState(false);
  const appsQuery = useQuery({
    queryKey: ["xct-apps", accessToken],
    enabled: Boolean(accessToken),
    queryFn: async (): Promise<AppRow[]> => {
      const data = await listXCTApps(accessToken);
      return Array.isArray(data) ? data : [];
    },
  });
  const rows = appsQuery.data ?? [];
  const loading = appsQuery.isFetching;
  const refresh = async () => {
    setPage(0);
    await appsQuery.refetch();
  };
  const copy = async (value: string) => {
    try {
      await navigator.clipboard.writeText(value);
      toast.success("Copied");
    } catch {
      toast.error("Copy failed. Select and copy the value manually.");
    }
  };
  const act = async (row: AppRow, action: "rotate" | "delete" | "toggle") => {
    if (busy) return;
    if (
      action !== "toggle" &&
      !window.confirm(
        action === "rotate"
          ? `Rotate the secret for ${row.display_name}? The old secret will stop working immediately.`
          : `Delete ${row.display_name}? OAuth credentials will become invalid and access tokens cannot be refreshed.`,
      )
    )
      return;
    setBusy(true);
    try {
      if (action === "rotate") {
        const result = await rotateXCTAppSecret(accessToken, row.app_id);
        setShowSecret(false);
        setReveal({ secret: result.client_secret, clientId: result.oauth_client_id });
      } else if (action === "delete") await deleteXCTApp(accessToken, row.app_id);
      else await patchXCTApp(accessToken, row.app_id, { is_active: !row.is_active });
      await refresh();
    } catch {
      toast.error("App operation failed");
    } finally {
      setBusy(false);
    }
  };
  const create = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (busy) return;
    const form = new FormData(event.currentTarget);
    const value = (key: string) => String(form.get(key) ?? "").trim();
    setBusy(true);
    try {
      const payload = {
        app_name: value("app_name"),
        display_name: value("display_name"),
        description: value("description"),
        redirect_uris: value("redirect_uris")
          .split("\n")
          .map((uri) => uri.trim())
          .filter(Boolean),
        default_team_id: value("default_team_id") || undefined,
        default_scopes: value("default_scopes")
          .split(/[\s,]+/)
          .filter(Boolean),
        capability_scope_id: value("capability_scope_id") || undefined,
        rpm_limit: value("rpm_limit") ? Number(value("rpm_limit")) : undefined,
        daily_budget: value("daily_budget") ? Number(value("daily_budget")) : undefined,
      };
      const result = await createXCTApp(accessToken, payload);
      setCreateOpen(false);
      setShowSecret(false);
      setReveal({ secret: result.client_secret, clientId: result.oauth_client_id });
      await refresh();
    } catch {
      toast.error("App creation failed");
    } finally {
      setBusy(false);
    }
  };
  return (
    <section className="space-y-4 rounded-lg border p-4">
      <h2 className="text-xl font-semibold">XCT Apps</h2>
      {appsQuery.isError && <p role="alert">Failed to load apps. Try refreshing.</p>}
      <div className="flex gap-2">
        <Button variant="outline" onClick={refresh} disabled={loading}>
          Refresh
        </Button>
        <Button onClick={() => setCreateOpen(true)} disabled={busy}>
          New app
        </Button>
      </div>
      {loading && <p role="status">Loading apps…</p>}
      <div className="overflow-x-auto">
        <table className="w-full text-left text-sm">
          <thead>
            <tr>
              {["App", "Client ID", "Redirect URIs", "Scope", "Active", "Actions"].map((heading) => (
                <th className="p-2" key={heading}>
                  {heading}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {rows.slice(page * 20, page * 20 + 20).map((row) => (
              <tr className="border-t" key={row.app_id}>
                <td className="p-2">
                  {row.display_name}
                  <div>{row.app_name}</div>
                </td>
                <td className="p-2">
                  <code>{row.oauth_client_id}</code>
                  <Button variant="ghost" onClick={() => copy(row.oauth_client_id)}>
                    Copy ID
                  </Button>
                </td>
                <td>{row.redirect_uris?.map((uri) => <div key={uri}>{uri}</div>)}</td>
                <td>{row.capability_scope_id ?? "—"}</td>
                <td>
                  <input
                    type="checkbox"
                    aria-label={`Active: ${row.display_name}`}
                    checked={row.is_active}
                    disabled={busy}
                    onChange={() => act(row, "toggle")}
                  />
                </td>
                <td className="flex gap-2 p-2">
                  <Button variant="outline" disabled={busy} onClick={() => act(row, "rotate")}>
                    Rotate secret
                  </Button>
                  <Button variant="destructive" disabled={busy} onClick={() => act(row, "delete")}>
                    Delete
                  </Button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {!loading && rows.length === 0 && <p>No apps yet.</p>}
      <div className="flex gap-3">
        <Button variant="outline" disabled={!page} onClick={() => setPage(page - 1)}>
          Previous
        </Button>
        <span>Page {page + 1}</span>
        <Button variant="outline" disabled={(page + 1) * 20 >= rows.length} onClick={() => setPage(page + 1)}>
          Next
        </Button>
      </div>
      <Dialog
        open={createOpen}
        onOpenChange={(open) => {
          if (!busy) setCreateOpen(open);
        }}
      >
        <DialogContent className="max-h-[85vh] overflow-y-auto sm:max-w-xl">
          <DialogTitle>Create XCT App</DialogTitle>
          <form onSubmit={create} className="space-y-3">
            {[
              ["app_name", "App name (unique slug)"],
              ["display_name", "Display name"],
              ["default_team_id", "Default team ID"],
              ["default_scopes", "Default scopes (space or comma separated)"],
              ["capability_scope_id", "Capability scope ID"],
            ].map(([name, label]) => (
              <label className="grid gap-1" key={name}>
                {label}
                <Input name={name} required={name === "app_name" || name === "display_name"} />
              </label>
            ))}
            <label className="grid gap-1">
              Description
              <Textarea name="description" rows={2} />
            </label>
            <label className="grid gap-1">
              Redirect URIs (one per line, exact match)
              <Textarea name="redirect_uris" rows={3} />
            </label>
            <label className="grid gap-1">
              RPM limit
              <Input name="rpm_limit" type="number" min={0} step={1} />
            </label>
            <label className="grid gap-1">
              Daily budget (USD)
              <Input name="daily_budget" type="number" min={0} step="any" />
            </label>
            <Button type="submit" disabled={busy}>
              {busy ? "Creating…" : "Create"}
            </Button>
          </form>
        </DialogContent>
      </Dialog>
      <Dialog open={reveal !== null}>
        <DialogContent showCloseButton={false}>
          <DialogTitle>Save this secret now</DialogTitle>
          <p>The proxy stores only a hash. This secret will not be shown again.</p>
          {reveal && (
            <>
              <label className="grid gap-1">
                OAuth client ID
                <Input readOnly value={reveal.clientId} />
              </label>
              <Button variant="outline" onClick={() => copy(reveal.clientId)}>
                Copy client ID
              </Button>
              <label className="grid gap-1">
                Client secret
                <Input type={showSecret ? "text" : "password"} readOnly value={reveal.secret} />
              </label>
              <Button variant="outline" onClick={() => setShowSecret(!showSecret)}>
                {showSecret ? "Hide secret" : "Show secret"}
              </Button>
              <Button variant="outline" onClick={() => copy(reveal.secret)}>
                Copy secret
              </Button>
              <Button onClick={() => setReveal(null)}>I&apos;ve saved it</Button>
            </>
          )}
        </DialogContent>
      </Dialog>
    </section>
  );
}
