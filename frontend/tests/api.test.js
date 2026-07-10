import test from "node:test";
import assert from "node:assert/strict";
import { ApiError, resolveDownloadUrl } from "../src/api.js";

test("direct download URLs stay on the app origin and expected endpoint", () => {
  assert.equal(
    resolveDownloadUrl("/api/files/job_ABC-123", "https://media.example"),
    "https://media.example/api/files/job_ABC-123",
  );
});

test("direct download URLs reject external, query and malformed targets", () => {
  for (const value of [
    "https://evil.example/api/files/job",
    "/api/files/job?token=secret",
    "/api/files/job/extra",
    "/not-a-file/job",
  ]) {
    assert.throws(
      () => resolveDownloadUrl(value, "https://media.example"),
      (error) => error instanceof ApiError && error.status === 500,
    );
  }
});
