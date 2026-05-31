import { mkdirSync } from "node:fs";
import path from "node:path";
import BetterSqliteDatabase from "better-sqlite3";
import { Kysely, SqliteDialect } from "kysely";
import { loadConfig } from "../config.js";
import type { Database as IrisDatabase } from "./types.js";

let db: Kysely<IrisDatabase> | null = null;
let sqliteDatabase: BetterSqliteDatabase.Database | null = null;

const jsonColumns = new Set([
  "settings",
  "hardware_info",
  "words",
  "data",
  "request",
  "result",
  "response",
  "metadata",
  "important_points",
  "action_items",
  "source_segment_ids",
]);

const booleanColumns = new Set(["is_interim"]);

function normalizeSqliteParam(value: unknown): unknown {
  if (value instanceof Date) return value.toISOString();
  if (typeof value === "boolean") return value ? 1 : 0;
  if (Array.isArray(value)) return JSON.stringify(value);
  if (
    value
    && typeof value === "object"
    && !(value instanceof Uint8Array)
    && !(value instanceof ArrayBuffer)
  ) {
    return JSON.stringify(value);
  }
  return value;
}

function parseJson(value: unknown) {
  if (typeof value !== "string") return value;
  try {
    return JSON.parse(value);
  } catch {
    return value;
  }
}

function normalizeSqliteRow(row: unknown) {
  if (!row || typeof row !== "object") return row;
  const normalized: Record<string, unknown> = { ...(row as Record<string, unknown>) };
  for (const [key, value] of Object.entries(normalized)) {
    if (jsonColumns.has(key)) normalized[key] = parseJson(value);
    if (booleanColumns.has(key)) normalized[key] = Boolean(value);
  }
  return normalized;
}

function wrapSqliteDatabase(database: BetterSqliteDatabase.Database) {
  return {
    close: () => database.close(),
    prepare(sql: string) {
      const statement = database.prepare(sql);
      return {
        get reader() {
          return statement.reader;
        },
        all(parameters: readonly unknown[]) {
          return statement.all(parameters.map(normalizeSqliteParam)).map(normalizeSqliteRow);
        },
        run(parameters: readonly unknown[]) {
          return statement.run(parameters.map(normalizeSqliteParam));
        },
        *iterate(parameters: readonly unknown[]) {
          for (const row of statement.iterate(parameters.map(normalizeSqliteParam))) {
            yield normalizeSqliteRow(row);
          }
        },
      };
    },
  };
}

function createSqliteDatabase(filePath: string) {
  mkdirSync(path.dirname(filePath), { recursive: true });
  const database = new BetterSqliteDatabase(filePath);
  database.pragma("journal_mode = WAL");
  database.pragma("foreign_keys = ON");
  sqliteDatabase = database;
  return wrapSqliteDatabase(database);
}

export function databaseDriver() {
  return "sqlite" as const;
}

export function isSqlite() {
  return databaseDriver() === "sqlite";
}

export function getSqliteDatabase() {
  if (!sqliteDatabase) {
    throw new Error("SQLite database has not been initialized");
  }
  return sqliteDatabase;
}

export function getDb() {
  if (db) return db;
  const config = loadConfig();
  db = new Kysely<IrisDatabase>({
    dialect: new SqliteDialect({
      database: createSqliteDatabase(config.sqlitePath),
    }),
  });
  return db;
}

export async function closeDb() {
  if (!db) return;
  const current = db;
  db = null;
  sqliteDatabase = null;
  await current.destroy();
}
