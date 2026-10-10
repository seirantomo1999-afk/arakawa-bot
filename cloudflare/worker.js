// Cron only dispatches the existing GitHub workflow; browsers stay on GitHub.
const API = "https://api.github.com";

function enabled(value) {
  return value === true || value === "true";
}

function githubFailure(response, operation) {
  const status = response.status;
  let code = "github_request_failed";
  let hint = "Check GitHub Actions history before trying again.";
  if (status === 429 || (status === 403 &&
      (response.headers.get("x-ratelimit-remaining") === "0" || response.headers.has("retry-after")))) {
    code = "github_rate_limited";
    hint = "GitHub rate limit reached; check retry-after/x-ratelimit-reset before trying again.";
  } else if (status === 401) {
    code = "github_token_invalid";
    hint = "Replace the expired, revoked, or invalid GitHub token in the Cloudflare GITHUB_TOKEN secret.";
  } else if (status === 403) {
    code = "github_access_denied";
    hint = "Check token repository selection and Actions: Read and write; also check repository policies and GitHub rate limits.";
  } else if (status === 404) {
    code = "github_resource_unavailable";
    hint = "Check repository/workflow names and token repository access; 404 alone does not prove the workflow is missing.";
  } else if (status === 422) {
    code = "github_dispatch_configuration";
    hint = "Check ref, workflow_dispatch, and inputs on the default branch; merge the migration PR after configuring bot Secrets.";
  }
  const error = new Error(`GitHub ${operation} failed (${status}): ${hint}`);
  Object.assign(error, { code, operation, httpStatus: status });
  return error;
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
    if (!response.ok) throw githubFailure(response, "run check");
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
  if (!response.ok) throw githubFailure(response, "dispatch");
  return { status: "accepted" };
}

export default {
  async scheduled(controller, env, ctx) {
    ctx.waitUntil(dispatch(env).then(result => {
      console.log(JSON.stringify({ ...result, scheduledTime: controller.scheduledTime }));
    }).catch(error => {
      // Fixed diagnostics only: never log tokens, request headers, or API bodies.
      console.error(JSON.stringify({
        status: "failed", scheduledTime: controller.scheduledTime,
        code: error.code || "scheduler_failed",
        operation: error.operation || "configuration_or_network",
        httpStatus: error.httpStatus || null,
      }));
      throw error;
    }));
  },
};
