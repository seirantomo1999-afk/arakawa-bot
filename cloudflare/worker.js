// Cron only dispatches the existing GitHub workflow; browsers stay on GitHub.
const API = "https://api.github.com";

function enabled(value) {
  return value === true || value === "true";
}

export async function dispatch(env, fetcher = fetch) {
  if (!enabled(env.ENABLED)) return { status: "disabled" };
  if (!env.GITHUB_TOKEN) throw new Error("GITHUB_TOKEN is not configured");
  const repository = env.GITHUB_REPOSITORY;
  if (!/^[\w.-]+\/[\w.-]+$/.test(repository || "")) {
    throw new Error("GITHUB_REPOSITORY must be owner/repo");
  }
  const workflow = encodeURIComponent(env.GITHUB_WORKFLOW || "notify.yml");
  const root = `${API}/repos/${repository}/actions/workflows/${workflow}`;
  const headers = {
    Accept: "application/vnd.github+json",
    Authorization: `Bearer ${env.GITHUB_TOKEN}`,
    "User-Agent": "arakawa-bot-scheduler",
    "X-GitHub-Api-Version": "2022-11-28",
  };
  // Avoid knowingly dispatching another scan while one is queued/running.
  // GitHub concurrency is the final lock for races between this check and POST.
  for (const status of ["queued", "in_progress", "waiting", "pending", "requested"]) {
    const response = await fetcher(`${root}/runs?status=${status}&per_page=1`, {
      headers, signal: AbortSignal.timeout(15000), redirect: "error",
    });
    if (!response.ok) throw new Error(`GitHub run check failed (${response.status})`);
    const data = await response.json();
    if (data.total_count > 0) return { status: "busy" };
  }
  const response = await fetcher(`${root}/dispatches`, {
    method: "POST", headers: { ...headers, "Content-Type": "application/json" },
    body: JSON.stringify({
      ref: env.GITHUB_REF || "main",
      inputs: { source: "cloudflare", dry_run: !enabled(env.BOOKING_ENABLED) },
    }),
    signal: AbortSignal.timeout(15000), redirect: "error",
  });
  // Do not retry POST: a timeout may occur after GitHub accepted the dispatch.
  if (!response.ok) throw new Error(`GitHub dispatch failed (${response.status})`);
  return { status: "accepted" };
}

export default {
  async scheduled(controller, env, ctx) {
    ctx.waitUntil(dispatch(env).then(result => {
      console.log(JSON.stringify({ ...result, scheduledTime: controller.scheduledTime }));
    }));
  },
};
