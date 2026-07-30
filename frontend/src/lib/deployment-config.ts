import { apiRequest } from "@/lib/api-wrapper"
import { resolveApiSnippetBaseUrl } from "@/lib/api-snippet-target"
import { getApiUrl } from "@/lib/utils"

export interface DeploymentConfig {
  /**
   * Origin external API and widget clients should call. Hosting layers may
   * advertise a region-pinned origin that differs from the owner's browser.
   */
  deployment_origin: string | null
  /** Canonical application origin used for browser share links. */
  app_origin: string | null
  /**
   * Optional routing region. When present, share links first visit the
   * canonical region bootstrap so a new recipient does not need prior state.
   */
  region: string | null
}

/** Load deployment targets from the backend serving the current owner. */
export async function fetchDeploymentConfig(): Promise<DeploymentConfig> {
  const response = await apiRequest(`${getApiUrl()}/api/deployment-config`)
  if (!response.ok) {
    throw new Error("Failed to load deployment configuration")
  }
  return response.json()
}

/**
 * Resolve an external-client origin only after deployment configuration has
 * loaded. A configured object with no override represents standalone XAgent,
 * where the browser origin remains the correct target.
 */
export function resolveDeploymentOrigin(
  config: DeploymentConfig | null,
  browserOrigin: string,
): string {
  if (!config) return ""
  return resolveApiSnippetBaseUrl(
    config.deployment_origin || browserOrigin,
    browserOrigin,
  )
}

/**
 * Build a public share URL without exposing the full application on a hidden
 * regional origin.
 *
 * Standalone deployments use their ordinary application URL. Multi-region
 * hosts advertise a region and canonical application origin, causing the
 * recipient to establish routing state through `/change-region` before the
 * browser opens `/share/<token>`.
 */
export function buildDeploymentShareUrl(
  shareToken: string,
  config: DeploymentConfig | null,
  browserOrigin: string,
): string {
  if (!shareToken || !config) return ""

  const sharePath = `/share/${encodeURIComponent(shareToken)}`
  const appOrigin = resolveApiSnippetBaseUrl(
    config.app_origin || browserOrigin,
    browserOrigin,
  )
  if (!appOrigin) return ""

  if (!config.region) {
    return new URL(sharePath, appOrigin).toString()
  }

  const bootstrap = new URL("/change-region", appOrigin)
  bootstrap.searchParams.set("region", config.region)
  bootstrap.searchParams.set("next", sharePath)
  return bootstrap.toString()
}
