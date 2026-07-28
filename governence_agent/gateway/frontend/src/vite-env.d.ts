/// <reference types="vite/client" />

interface ImportMetaEnv {
  /**
   * Base URL prefixed onto every gateway path. Empty string = same-origin,
   * which is the correct value for both dev (via the Vite proxy) and the
   * standard production deployment (gateway serves this bundle). See .env.
   */
  readonly VITE_API_BASE_URL: string
}

interface ImportMeta {
  readonly env: ImportMetaEnv
}
