import { useState, type FormEvent } from "react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { createXCTSkill, uploadXCTSkillZip } from "../networking";

interface Props {
  accessToken: string;
  onSuccess: (skill: { skill_id?: string; display_title?: string }) => void;
}

export default function SkillUploadForm({ accessToken, onSuccess }: Props) {
  const [submitting, setSubmitting] = useState(false);
  const [file, setFile] = useState<File | null>(null);
  const [isPublic, setIsPublic] = useState(false);
  const [error, setError] = useState("");
  const submitZip = async (event: FormEvent) => {
    event.preventDefault();
    if (!file || submitting) return;
    if (file.size > 10 * 1024 * 1024) {
      setError("ZIP files must be 10 MB or smaller.");
      return;
    }
    setSubmitting(true);
    setError("");
    try {
      onSuccess(await uploadXCTSkillZip(accessToken, file, isPublic ? { is_public_override: true } : {}));
    } catch {
      setError("Skill upload failed. Please try again.");
    } finally {
      setSubmitting(false);
    }
  };
  const submitManual = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (submitting) return;
    const values = new FormData(event.currentTarget);
    const schemaText = String(values.get("tool_schema") || "").trim();
    const schema = (() => {
      try {
        return schemaText ? (JSON.parse(schemaText) as unknown) : undefined;
      } catch {
        return null;
      }
    })();
    if (schemaText && !Array.isArray(schema)) {
      setError("Tool schema must be a valid JSON array.");
      return;
    }
    setSubmitting(true);
    setError("");
    try {
      const payload = {
        display_title: String(values.get("display_title")).trim(),
        description: String(values.get("description") || ""),
        version: String(values.get("version") || "1"),
        is_public: values.get("is_public") === "on",
        system_prompt_template: String(values.get("system_prompt_template") || ""),
        tool_schema: schema,
      };
      onSuccess(await createXCTSkill(accessToken, payload));
    } catch {
      setError("Skill creation failed. Please try again.");
    } finally {
      setSubmitting(false);
    }
  };
  return (
    <div className="space-y-4">
      {error && (
        <p role="alert" className="text-destructive">
          {error}
        </p>
      )}
      <Tabs defaultValue="zip">
        <TabsList>
          <TabsTrigger value="zip">Upload ZIP</TabsTrigger>
          <TabsTrigger value="manual">Manual</TabsTrigger>
        </TabsList>
        <TabsContent value="zip">
          <form onSubmit={submitZip} className="space-y-4">
            <p>
              Include manifest.yaml at the archive root. SKILL.md, tools.json and README.md are optional. Maximum size:
              10 MB.
            </p>
            <label
              className="grid gap-2"
              onDragOver={(event) => event.preventDefault()}
              onDrop={(event) => {
                event.preventDefault();
                setFile(event.dataTransfer.files[0] ?? null);
              }}
            >
              Skill ZIP (choose or drop a file)
              <Input
                type="file"
                accept=".zip,application/zip"
                onChange={(event) => setFile(event.target.files?.[0] ?? null)}
              />
            </label>
            {file && (
              <p>
                {file.name}{" "}
                <Button type="button" variant="ghost" onClick={() => setFile(null)}>
                  Remove file
                </Button>
              </p>
            )}
            <label className="flex gap-2">
              <input type="checkbox" checked={isPublic} onChange={(event) => setIsPublic(event.target.checked)} />
              Override manifest visibility to public
            </label>
            <Button type="submit" disabled={!file || submitting}>
              {submitting ? "Uploading…" : "Upload"}
            </Button>
          </form>
        </TabsContent>
        <TabsContent value="manual">
          <form onSubmit={submitManual} className="space-y-4">
            <label className="grid gap-2">
              Display title
              <Input name="display_title" required />
            </label>
            <label className="grid gap-2">
              Description
              <Textarea name="description" rows={2} />
            </label>
            <label className="grid gap-2">
              Version
              <Input name="version" defaultValue="1" />
            </label>
            <label className="flex gap-2">
              <input type="checkbox" name="is_public" />
              Public (visible in anonymous capability discovery)
            </label>
            <label className="grid gap-2">
              System prompt template
              <Textarea name="system_prompt_template" rows={8} />
            </label>
            <label className="grid gap-2">
              Tool schema (JSON array)
              <Textarea name="tool_schema" rows={6} />
            </label>
            <Button type="submit" disabled={submitting}>
              {submitting ? "Creating…" : "Create skill"}
            </Button>
            <p>Publish from the list to freeze the content fields.</p>
          </form>
        </TabsContent>
      </Tabs>
    </div>
  );
}
