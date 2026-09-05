import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Dialog, DialogContent, DialogTitle } from "@/components/ui/dialog";
import { toast } from "@/lib/toast";
import { deleteXCTSkill, getXCTSkill, listXCTSkills, publishXCTSkill } from "../networking";
import SkillUploadForm from "./SkillUploadForm";

interface SkillRow {
  skill_id: string;
  display_title?: string;
  description?: string;
  version?: string;
  source?: string;
  team_id?: string;
  is_public?: boolean;
  xct_metadata?: Record<string, unknown>;
  created_by?: string;
  tool_schema?: unknown[];
  system_prompt_template?: string;
}
export default function SkillTable({ accessToken, isAdmin = false }: { accessToken: string; isAdmin?: boolean }) {
  const [search, setSearch] = useState("");
  const [page, setPage] = useState(0);
  const [detail, setDetail] = useState<SkillRow | null>(null);
  const [upload, setUpload] = useState(false);
  const [busy, setBusy] = useState(false);
  const [submittedSearch, setSubmittedSearch] = useState("");
  const skillsQuery = useQuery({
    queryKey: ["xct-skills", accessToken, submittedSearch],
    enabled: Boolean(accessToken),
    queryFn: async (): Promise<SkillRow[]> => {
      const result = await listXCTSkills(accessToken, submittedSearch ? { q: submittedSearch } : {});
      return result?.data ?? [];
    },
  });
  const rows = skillsQuery.data ?? [];
  const loading = skillsQuery.isFetching;
  const refresh = async (query = "") => {
    setPage(0);
    setSubmittedSearch(query);
    if (query === submittedSearch) await skillsQuery.refetch();
  };
  const onUploaded = () => {
    setUpload(false);
    toast.success("Skill created");
    void refresh(search);
  };
  const mutate = async (row: SkillRow, action: "delete" | "publish") => {
    if (!isAdmin || busy) return;
    if (
      action === "delete" &&
      !window.confirm(
        `Delete ${row.display_title ?? row.skill_id}? This cannot be undone and existing calls using the skill may fail.`,
      )
    )
      return;
    setBusy(true);
    try {
      if (action === "delete") await deleteXCTSkill(accessToken, row.skill_id);
      else await publishXCTSkill(accessToken, row.skill_id);
      toast.success(action === "delete" ? "Skill deleted" : "Skill published; content is now immutable");
      await refresh(search);
    } catch {
      toast.error("Skill operation failed");
    } finally {
      setBusy(false);
    }
  };
  const view = async (id: string) => {
    try {
      setDetail(await getXCTSkill(accessToken, id));
    } catch {
      toast.error("Failed to load skill");
    }
  };
  return (
    <section className="space-y-4 rounded-lg border p-4">
      <h2 className="text-xl font-semibold">XCT Skills</h2>
      {skillsQuery.isError && <p role="alert">Failed to load skills. Try refreshing.</p>}
      <form
        className="flex flex-wrap gap-2"
        onSubmit={(event) => {
          event.preventDefault();
          void refresh(search);
        }}
      >
        <Input
          aria-label="Search skills"
          className="max-w-sm"
          placeholder="Search title / description"
          value={search}
          onChange={(event) => setSearch(event.target.value)}
        />
        <Button type="submit" disabled={loading}>
          Refresh
        </Button>
        {isAdmin && (
          <Button type="button" onClick={() => setUpload(true)}>
            Upload ZIP / Create
          </Button>
        )}
      </form>
      {loading && <p role="status">Loading skills…</p>}
      <div className="overflow-x-auto">
        <table className="w-full text-left text-sm">
          <thead>
            <tr>
              {["Title", "Version", "Source", "Public", "Team", "Owner", "Actions"].map((label) => (
                <th className="p-2" key={label}>
                  {label}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {rows.slice(page * 20, page * 20 + 20).map((row) => (
              <tr className="border-t" key={row.skill_id}>
                <td className="p-2">
                  {row.display_title ?? "(untitled)"}
                  <div className="text-muted-foreground">{row.skill_id}</div>
                </td>
                <td className="p-2">
                  {row.version ?? "1"}
                  {row.xct_metadata?.published === true && " · published"}
                </td>
                <td>{row.source ?? "custom"}</td>
                <td>{row.is_public ? "public" : "private"}</td>
                <td>{row.team_id}</td>
                <td>{row.created_by}</td>
                <td className="flex gap-2 p-2">
                  <Button variant="outline" onClick={() => view(row.skill_id)}>
                    View
                  </Button>
                  {isAdmin && (
                    <>
                      {!row.xct_metadata?.published && (
                        <Button disabled={busy} onClick={() => mutate(row, "publish")}>
                          Publish
                        </Button>
                      )}
                      <Button variant="destructive" disabled={busy} onClick={() => mutate(row, "delete")}>
                        Delete
                      </Button>
                    </>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {!loading && rows.length === 0 && <p>No skills found.</p>}
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
        open={detail !== null}
        onOpenChange={(open) => {
          if (!open) setDetail(null);
        }}
      >
        <DialogContent className="max-h-[85vh] overflow-y-auto sm:max-w-2xl">
          <DialogTitle>{detail?.display_title ?? "Skill"}</DialogTitle>
          {detail && (
            <>
              <p>{detail.description}</p>
              <h3>Metadata</h3>
              <pre className="overflow-auto">{JSON.stringify(detail.xct_metadata ?? {}, null, 2)}</pre>
              <h3>System prompt template</h3>
              <pre className="whitespace-pre-wrap">{detail.system_prompt_template}</pre>
              <h3>Tool schema</h3>
              <pre className="overflow-auto">{JSON.stringify(detail.tool_schema ?? [], null, 2)}</pre>
            </>
          )}
        </DialogContent>
      </Dialog>
      <Dialog open={upload} onOpenChange={setUpload}>
        <DialogContent className="max-h-[85vh] overflow-y-auto sm:max-w-2xl">
          <DialogTitle>Create skill</DialogTitle>
          {upload && <SkillUploadForm accessToken={accessToken} onSuccess={onUploaded} />}
        </DialogContent>
      </Dialog>
    </section>
  );
}
