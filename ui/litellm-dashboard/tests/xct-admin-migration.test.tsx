import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactElement } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render as renderComponent, screen, waitFor } from "@testing-library/react";
import SkillUploadForm from "@/components/skills/SkillUploadForm";
import SkillTable from "@/components/skills/SkillTable";
import AppTable from "@/components/apps/AppTable";
import Marketplace from "@/components/agents/XctAgentMarketplace";
import ByAppUsage from "@/components/usage/ByAppUsage";

const render = (element: ReactElement) => {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return renderComponent(<QueryClientProvider client={client}>{element}</QueryClientProvider>);
};

const api = vi.hoisted(() => ({
  createXCTSkill: vi.fn(),
  uploadXCTSkillZip: vi.fn(),
  listXCTSkills: vi.fn(),
  getXCTSkill: vi.fn(),
  publishXCTSkill: vi.fn(),
  deleteXCTSkill: vi.fn(),
  listXCTApps: vi.fn(),
  createXCTApp: vi.fn(),
  deleteXCTApp: vi.fn(),
  patchXCTApp: vi.fn(),
  rotateXCTAppSecret: vi.fn(),
  createAgentCall: vi.fn(),
  getUsageByApp: vi.fn(),
}));
vi.mock("@/components/networking", () => api);
vi.mock("@/lib/toast", () => ({ toast: { error: vi.fn(), success: vi.fn() } }));
beforeEach(() => {
  vi.clearAllMocks();
  api.listXCTSkills.mockResolvedValue({ data: [] });
  api.listXCTApps.mockResolvedValue([]);
  api.createXCTSkill.mockResolvedValue({ skill_id: "created" });
});
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("custom admin migration", () => {
  it("rejects invalid manual tool JSON and preserves a valid skill payload", async () => {
    const onSuccess = vi.fn();
    render(<SkillUploadForm accessToken="token" onSuccess={onSuccess} />);
    fireEvent.click(screen.getByRole("tab", { name: "Manual" }));
    fireEvent.change(screen.getByLabelText("Display title"), { target: { value: "Fact checker" } });
    fireEvent.change(screen.getByLabelText("Tool schema (JSON array)"), { target: { value: "{" } });
    fireEvent.click(screen.getByRole("button", { name: "Create skill" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("valid JSON array");
    expect(api.createXCTSkill).not.toHaveBeenCalled();
    fireEvent.change(screen.getByLabelText("Tool schema (JSON array)"), { target: { value: '[{"type":"function"}]' } });
    fireEvent.click(screen.getByRole("button", { name: "Create skill" }));
    await waitFor(() => expect(onSuccess).toHaveBeenCalledWith({ skill_id: "created" }));
    expect(api.createXCTSkill).toHaveBeenCalledWith(
      "token",
      expect.objectContaining({
        display_title: "Fact checker",
        version: "1",
        tool_schema: [{ type: "function" }],
        is_public: false,
      }),
    );
  });

  it("uploads the chosen ZIP with explicit public override", async () => {
    api.uploadXCTSkillZip.mockResolvedValue({ skill_id: "zip" });
    const success = vi.fn();
    render(<SkillUploadForm accessToken="token" onSuccess={success} />);
    const file = new File(["zip"], "skill.zip", { type: "application/zip" });
    fireEvent.change(screen.getByLabelText(/Skill ZIP/), { target: { files: [file] } });
    fireEvent.click(screen.getByLabelText("Override manifest visibility to public"));
    fireEvent.click(screen.getByRole("button", { name: "Upload", exact: true }));
    await waitFor(() =>
      expect(api.uploadXCTSkillZip).toHaveBeenCalledWith("token", file, { is_public_override: true }),
    );
  });

  it("retains skill detail and administrative publish actions", async () => {
    api.listXCTSkills.mockResolvedValue({ data: [{ skill_id: "s", display_title: "Search", xct_metadata: {} }] });
    api.getXCTSkill.mockResolvedValue({
      skill_id: "s",
      display_title: "Search",
      system_prompt_template: "Verify sources",
      tool_schema: [],
    });
    render(<SkillTable accessToken="token" isAdmin />);
    fireEvent.click(await screen.findByRole("button", { name: "Publish" }));
    await waitFor(() => expect(api.publishXCTSkill).toHaveBeenCalledWith("token", "s"));
    fireEvent.click(screen.getByRole("button", { name: "View" }));
    expect(await screen.findByText("Verify sources")).toBeVisible();
  });

  it("creates app scopes and zero limits and only reveals the returned secret until acknowledged", async () => {
    api.createXCTApp.mockResolvedValue({ oauth_client_id: "client", client_secret: "one-time-secret" });
    render(<AppTable accessToken="token" />);
    fireEvent.click(screen.getByRole("button", { name: "New app" }));
    fireEvent.change(screen.getByLabelText("App name (unique slug)"), { target: { value: "app" } });
    fireEvent.change(screen.getByLabelText("Display name"), { target: { value: "My app" } });
    fireEvent.change(screen.getByLabelText("Default scopes (space or comma separated)"), {
      target: { value: "read, write" },
    });
    fireEvent.change(screen.getByLabelText("RPM limit"), { target: { value: "0" } });
    fireEvent.click(screen.getByRole("button", { name: "Create", exact: true }));
    expect(await screen.findByLabelText("Client secret")).toHaveValue("one-time-secret");
    expect(api.createXCTApp).toHaveBeenCalledWith(
      "token",
      expect.objectContaining({ default_scopes: ["read", "write"], rpm_limit: 0 }),
    );
    fireEvent.click(screen.getByRole("button", { name: "I've saved it" }));
    await waitFor(() => expect(screen.queryByLabelText("Client secret")).not.toBeInTheDocument());
  });

  it("imports the selected A2A card and keeps its URL trailing slash", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string) =>
        Response.json(
          url.endsWith("/agents")
            ? { agents: [{ slug: "helper", name: "Helper", description: "Helps", category: "work" }] }
            : { version: "1", capabilities: { streaming: true }, skills: [], description: "Long description" },
        ),
      ),
    );
    const added = vi.fn();
    render(<Marketplace accessToken="token" isAdmin gatewayUrl="https://agents.test" onAgentAdded={added} />);
    fireEvent.click(await screen.findByRole("button", { name: /Helper Helps/ }));
    const button = await screen.findByRole("button", { name: "Add to LiteLLM" });
    await waitFor(() => expect(button).toBeEnabled());
    fireEvent.click(button);
    await waitFor(() => expect(added).toHaveBeenCalled());
    expect(api.createAgentCall).toHaveBeenCalledWith(
      "token",
      expect.objectContaining({
        agent_name: "xct-helper",
        agent_card_params: expect.objectContaining({
          url: "https://agents.test/agents/helper/",
          capabilities: { streaming: true },
        }),
      }),
    );
  });

  it("filters app usage without incorrectly carrying whole-app tokens into a subset", async () => {
    api.getUsageByApp.mockResolvedValue([
      {
        app_id: "app",
        total_spend: 10,
        total_requests: 20,
        total_tokens: 500,
        entity_breakdown: { skill: { spend: 2, requests: 3 } },
      },
    ]);
    render(<ByAppUsage accessToken="token" />);
    expect(await screen.findByText("500")).toBeVisible();
    fireEvent.change(screen.getByLabelText("Entity type"), { target: { value: "skill" } });
    expect(screen.getByText("$2.0000")).toBeVisible();
    expect(screen.queryByText("500")).not.toBeInTheDocument();
    expect(screen.getByText("—")).toBeVisible();
  });
});

it("does not expose skill mutation controls to a non-admin", async () => {
  api.listXCTSkills.mockResolvedValue({ data: [{ skill_id: "s", display_title: "Private", xct_metadata: {} }] });
  render(<SkillTable accessToken="token" />);
  expect(await screen.findByRole("button", { name: "View" })).toBeVisible();
  expect(screen.queryByRole("button", { name: "Publish" })).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "Delete" })).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: /Upload ZIP/ })).not.toBeInTheDocument();
});

it("retains app disabling and secret rotation with confirmation", async () => {
  const row = {
    app_id: "app-id",
    app_name: "app",
    display_name: "Work",
    oauth_client_id: "client",
    redirect_uris: [],
    is_active: true,
  };
  api.listXCTApps.mockResolvedValue([row]);
  api.rotateXCTAppSecret.mockResolvedValue({ oauth_client_id: "client", client_secret: "rotated-secret" });
  vi.stubGlobal(
    "confirm",
    vi.fn(() => true),
  );
  render(<AppTable accessToken="token" />);
  fireEvent.click(await screen.findByLabelText("Active: Work"));
  await waitFor(() => expect(api.patchXCTApp).toHaveBeenCalledWith("token", "app-id", { is_active: false }));
  await waitFor(() => expect(screen.getByRole("button", { name: "Rotate secret" })).toBeEnabled());
  fireEvent.click(screen.getByRole("button", { name: "Rotate secret" }));
  expect(await screen.findByLabelText("Client secret")).toHaveValue("rotated-secret");
  expect(window.confirm).toHaveBeenCalled();
});

it("reports missing usage data instead of presenting a successful empty response", async () => {
  api.getUsageByApp.mockRejectedValue(new Error("unavailable"));
  render(<ByAppUsage accessToken="token" />);
  expect(await screen.findByRole("alert")).toHaveTextContent("App usage is unavailable");
});

it("keeps the selected date range when an older usage request completes late", async () => {
  const previous = Promise.withResolvers<unknown>();
  api.getUsageByApp
    .mockReturnValueOnce(previous.promise)
    .mockResolvedValueOnce([{ app_id: "current-window", total_spend: 1, total_requests: 1 }]);
  render(<ByAppUsage accessToken="token" />);
  await waitFor(() => expect(api.getUsageByApp).toHaveBeenCalledTimes(1));
  fireEvent.change(screen.getByLabelText("Start date"), { target: { value: "2026-01-01" } });
  expect(await screen.findByText("current-window")).toBeVisible();
  previous.resolve([{ app_id: "stale-window", total_spend: 9, total_requests: 9 }]);
  await waitFor(() => expect(screen.queryByText("stale-window")).not.toBeInTheDocument());
  expect(screen.getByText("current-window")).toBeVisible();
});

it("blocks invalid usage date ranges before issuing a request", async () => {
  api.getUsageByApp.mockResolvedValue([]);
  render(<ByAppUsage accessToken="token" />);
  await waitFor(() => expect(api.getUsageByApp).toHaveBeenCalledTimes(1));
  fireEvent.change(screen.getByLabelText("Start date"), { target: { value: "2099-01-01" } });
  expect(screen.getByRole("alert")).toHaveTextContent("Choose a valid date range");
  fireEvent.click(screen.getByRole("button", { name: "Refresh" }));
  expect(api.getUsageByApp).toHaveBeenCalledTimes(1);
});
