import { test } from "node:test";
import assert from "node:assert/strict";
import worker, { dispatch } from "./worker.js";

const env = {
  ENABLED: "true", GITHUB_TOKEN: "fake-test-token",
  GITHUB_REPOSITORY: "example/arakawa-bot",
};
const empty = () => Response.json({ total_count: 0, workflow_runs: [] });

test("disabled scheduler makes no requests", async () => {
  assert.deepEqual(await dispatch({}, () => { throw new Error("unexpected request"); }), {status:"disabled"});
});

test("missing credentials fail without making a request", async () => {
  await assert.rejects(dispatch({ENABLED:"true"}), /GITHUB_TOKEN/);
});

test("dispatch defaults to dry-run and sends no token in its body", async () => {
  const calls = [];
  const result = await dispatch(env, async (url, init) => {
    calls.push([url, init]);
    return init.method === "POST" ? new Response(null, {status:204}) : empty();
  });
  assert.equal(result.status, "accepted");
  assert.equal(calls.filter(([,init]) => init.method === "POST").length, 1);
  const [url, init] = calls.at(-1);
  assert.match(url, /notify.yml\/dispatches$/);
  assert.deepEqual(JSON.parse(init.body), {ref:"main",inputs:{source:"cloudflare",dry_run:true}});
  assert.equal(init.body.includes(env.GITHUB_TOKEN), false);
});

test("booking must be explicitly enabled", async () => {
  let body;
  await dispatch({...env, BOOKING_ENABLED:"true"}, async (_, init) => {
    if (init.method !== "POST") return empty();
    body = JSON.parse(init.body);
    return new Response(null, {status:204});
  });
  assert.equal(body.inputs.dry_run, false);
});

test("a queued scan prevents another dispatch", async () => {
  let count = 0;
  assert.equal((await dispatch(env, async () => {
    count++;
    return Response.json({total_count:1});
  })).status, "busy");
  assert.equal(count, 1);
});

test("failed run check does not attempt POST", async () => {
  let count = 0;
  await assert.rejects(dispatch(env, async () => {
    count++;
    return new Response(null, {status:403});
  }), /403/);
  assert.equal(count, 1);
});

test("ambiguous dispatch failure is not retried", async () => {
  let posts = 0;
  await assert.rejects(dispatch(env, async (_, init) => {
    if (init.method !== "POST") return empty();
    posts++;
    throw new Error("timeout after acceptance");
  }), /timeout/);
  assert.equal(posts, 1);
});

test("Cron records a rejected invocation when dispatch fails", async (t) => {
  const errors = [];
  t.mock.method(console, "error", line => errors.push(JSON.parse(line)));
  let pending;
  await worker.scheduled({scheduledTime:0}, {ENABLED:"true"}, {
    waitUntil(promise) { pending = promise; },
  });
  await assert.rejects(pending, /GITHUB_TOKEN/);
  assert.equal(errors[0].status, "failed");
  assert.equal(errors[0].code, "scheduler_failed");
});

test("invalid token and inaccessible workflow have distinct diagnostics", async () => {
  for (const [status, code] of [[401,"github_token_invalid"], [404,"github_resource_unavailable"]]) {
    await assert.rejects(dispatch(env, async () => new Response(null, {status})), error => {
      assert.equal(error.code, code);
      assert.equal(error.operation, "run check");
      assert.equal(error.httpStatus, status);
      assert.equal(error.message.includes(env.GITHUB_TOKEN), false);
      return true;
    });
  }
});

test("permission denial is distinguished from explicit rate limits", async () => {
  for (const [status, headers, code] of [
    [403, {}, "github_access_denied"],
    [403, {"x-ratelimit-remaining":"0"}, "github_rate_limited"],
    [403, {"retry-after":"60"}, "github_rate_limited"],
    [429, {}, "github_rate_limited"],
  ]) {
    await assert.rejects(dispatch(env, async () => new Response(null, {status,headers})), error => {
      assert.equal(error.code, code);
      return true;
    });
  }
});

test("unmerged dispatch inputs fail once with configuration guidance", async () => {
  let posts = 0;
  await assert.rejects(dispatch(env, async (_, init) => {
    if (init.method !== "POST") return empty();
    posts++;
    return new Response(null, {status:422});
  }), error => {
    assert.equal(error.code, "github_dispatch_configuration");
    assert.match(error.message, /default branch/);
    return true;
  });
  assert.equal(posts, 1);
});

test("Cron failure log reports the operation without echoing GitHub response data", async (t) => {
  const errors = [];
  t.mock.method(console, "error", line => errors.push(JSON.parse(line)));
  t.mock.method(globalThis, "fetch", async () => new Response(env.GITHUB_TOKEN, {status:403}));
  let pending;
  await worker.scheduled({scheduledTime:123}, env, {waitUntil(promise) {pending=promise;}});
  await assert.rejects(pending, /403/);
  assert.deepEqual(errors, [{status:"failed",scheduledTime:123,code:"github_access_denied",operation:"run check",httpStatus:403}]);
  assert.equal(JSON.stringify(errors).includes(env.GITHUB_TOKEN), false);
});
