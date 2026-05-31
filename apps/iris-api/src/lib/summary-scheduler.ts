import { localUserContext } from "./auth.js";
import { errorMessage, logError, logInfo } from "./log.js";
import { generateDailySummaryForUser } from "./summaries.js";

const fifteenMinutesMs = 15 * 60 * 1000;
let summaryScheduler: NodeJS.Timeout | null = null;
let running = false;

function localDayRange(offsetDays: number) {
  const now = new Date();
  const start = new Date(now.getFullYear(), now.getMonth(), now.getDate() + offsetDays);
  const end = new Date(now.getFullYear(), now.getMonth(), now.getDate() + offsetDays + 1);
  return { periodStart: start, periodEnd: end };
}

async function generateScheduledDailySummaries() {
  if (running) return;
  running = true;
  try {
    const user = await localUserContext();
    const ranges = [localDayRange(-1), localDayRange(0)];
    for (const range of ranges) {
      const { summary, segmentCount } = await generateDailySummaryForUser({
        user,
        periodStart: range.periodStart,
        periodEnd: range.periodEnd,
      });
      logInfo("daily_summary_generated", {
        summaryId: summary.id,
        periodStart: range.periodStart.toISOString(),
        periodEnd: range.periodEnd.toISOString(),
        segmentCount,
      });
    }
  } catch (error) {
    logError("daily_summary_generation_failed", { error: errorMessage(error) });
  } finally {
    running = false;
  }
}

export function startSummaryScheduler() {
  if (summaryScheduler) return;
  summaryScheduler = setInterval(() => {
    void generateScheduledDailySummaries();
  }, fifteenMinutesMs);
  summaryScheduler.unref();
  setTimeout(() => {
    void generateScheduledDailySummaries();
  }, 10_000).unref();
  logInfo("summary_scheduler_started", { intervalMs: fifteenMinutesMs });
}
