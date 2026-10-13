// View-model types for the console — re-exported from the typed REST client
// so components always agree with the Python server's /api/* payloads.
export type {
  ServerStatus,
  SizingInfo,
  ToolRow,
  ToolState,
  ToolSource,
  LogLine,
  ConsoleConfig,
  ConfigValue,
  ConfigUpdate,
  ConfigUpdateResult,
  AuditEvent,
  ConversionRun,
  RepairIncident,
  Governance,
  PypiResult,
  PypiOk,
  Snippets,
  GenericHttpSnippet,
} from "@/api";
