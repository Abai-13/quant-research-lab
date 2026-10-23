export interface Health {
  service: string;
  name: string;
  version: string;
  mode: string;
  status: string;
}

export interface ReadyDatabaseReady {
  status: "ready";
  schema_version: number;
  instance_id: string;
  created_at: string;
}

export interface ReadyDatabaseNotReady {
  status: "not_ready";
  reason: string;
  message: string | null;
}

export type ReadyDatabase = ReadyDatabaseReady | ReadyDatabaseNotReady;

export interface Ready {
  status: "ready" | "not_ready";
  database: ReadyDatabase;
}

export type FeatureState =
  | "not_connected"
  | "not_initialized"
  | "not_implemented"
  | "not_configured"
  | "import_failed"
  | "pending_check"
  | "partial_coverage"
  | "limited"
  | "available"
  | "available_in_range"
  | "migration_required";

export interface FeatureStatus {
  status: FeatureState;
}

export interface AccountStatus extends FeatureStatus {
  net_value: null;
  cumulative_return: null;
}

export interface DataSourceSummary {
  source_code: string;
  api_base_url: string | null;
  credentials_present: boolean;
  is_fixture: boolean;
}

export interface DataCoverageEntry {
  count: number;
  start: string | null;
  end: string | null;
}

export interface DataQualitySummary {
  total: number;
  by_severity: {
    info: number;
    warn: number;
    high: number;
  };
}

export interface DataVersionSummary {
  version_id: string;
  published_at: string | null;
  manifest_sha256: string;
  rule_version: string;
}

export interface DataStatus {
  status: FeatureState;
  detail?: string;
  record_count?: number;
  source: DataSourceSummary;
  coverage: Record<string, DataCoverageEntry>;
  quality: DataQualitySummary;
  last_import: {
    batch_id: string;
    mode: string;
    status: string;
    started_at: string | null;
    finished_at: string | null;
    pages_ok: number;
    pages_failed: number;
    error_class: string | null;
  } | null;
  data_versions: DataVersionSummary[];
  pool_snapshots: { snapshot_id: string; as_of: string; member_count: number }[];
  limitations: string[];
}

export interface SystemStatus {
  service: string;
  name: string;
  version: string;
  mode: string;
  generated_at: string;
  market_data: FeatureStatus;
  account: AccountStatus;
  strategy: FeatureStatus;
  scheduler: FeatureStatus;
  ai: FeatureStatus;
  limitations: string[];
}
