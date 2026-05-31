import { initializeSqliteSchema } from "./sqlite-schema.js";

export async function initializeSchema() {
  await initializeSqliteSchema();
}
