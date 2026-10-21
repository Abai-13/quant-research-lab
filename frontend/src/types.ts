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
  | "not_configured";

export interface FeatureStatus {
  status: FeatureState;
}

export interface AccountStatus extends FeatureStatus {
  net_value: null;
  cumulative_return: null;
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
