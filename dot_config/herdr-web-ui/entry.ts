// launchd owns this process; tool updates go through the explicit chezmoi commands.
import { readFileSync } from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";
import { pathToFileURL } from "node:url";
import { randomUUID } from "node:crypto";

const root = join(homedir(), ".local/share/herdr-web-ui/current");
const version = JSON.parse(readFileSync(join(root, "package.json"), "utf8")).version;
process.env.HERDR_WEB_BOOT_ID = randomUUID();
const { createServer } = await import(pathToFileURL(join(root, "server/index.ts")).href);
const instance = createServer({
  updates: {
    status: () => ({
      managed: false, auto_update: false, phase: "idle", current_revision: process.env.HERDR_WEB_REVISION ?? null,
      current_version: version, latest_revision: null, latest_version: null, available: false,
      checked_at: null, error: null, blocked_reason: "Updates are managed by herdr-web-update on this Mac.",
    }),
    request: () => { throw new Error("Use herdr-web-update"); },
  },
  // No HerdrUpdater: the browser must not update/restart a service behind the Arc gate.
});
let stopping = false;
const shutdown = () => {
  if (stopping) return;
  stopping = true;
  instance.stop();
  setTimeout(() => process.exit(0), 2000);
};
process.on("SIGTERM", shutdown);
process.on("SIGINT", shutdown);
console.log(`herdr-web-ui listening on http://${instance.hostname}:${instance.port} (launchd)`);
