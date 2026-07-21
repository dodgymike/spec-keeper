/// <reference types="vite/client" />

interface ImportMetaEnv {
  /** Base URL of the Spec Server API, e.g. http://localhost:8080 */
  readonly VITE_API_BASE?: string;
  /**
   * Dev-only bearer token, used until Cognito JWT login is wired up.
   * Never set this in a production build.
   */
  readonly VITE_DEV_TOKEN?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
