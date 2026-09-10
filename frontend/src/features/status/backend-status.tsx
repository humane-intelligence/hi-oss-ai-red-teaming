import { useQuery } from '@tanstack/react-query'
import { apiClient } from '@/lib/api/client'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { useVersion } from '@/features/status/queries'
import type { ReadinessResponse } from '@/lib/api/types'

// /ready returns ReadinessResponse on both 200 and 503 — never use unwrap here.
async function fetchReady(): Promise<ReadinessResponse> {
  const { data, error, response } = await apiClient.GET('/ready')
  if (response.status === 200 && data !== undefined) return data
  // 503: backend is reachable but some checks failed — still structured data
  if (response.status === 503 && error !== undefined) return error as ReadinessResponse
  throw new Error(`Request failed (${response.status})`)
}

function useReady() {
  return useQuery({
    queryKey: ['ready'],
    queryFn: fetchReady,
    refetchInterval: 15000,
  })
}

function CheckRow({ name, result }: { name: string; result: ReadinessResponse['checks'][string] }) {
  return (
    <div className="py-2">
      <div className="flex items-center gap-3">
        <span
          className={`size-2 shrink-0 rounded-full ${result.ok ? 'bg-ok animate-pulse motion-reduce:animate-none' : 'bg-err'}`}
          aria-hidden="true"
        />
        <span className="font-mono text-sm">{name}</span>
        {/* The dot is decorative (aria-hidden); carry ok/failing to screen readers here. */}
        <span className="sr-only">{result.ok ? 'ok' : 'failing'}</span>
      </div>
      {/* Indented past the dot (size-2) + gap-3 so the error lines up under the label. */}
      {!result.ok && result.error && (
        <p className="text-destructive mt-0.5 pl-5 text-xs">{result.error}</p>
      )}
    </div>
  )
}

export function BackendStatusPage() {
  const ready = useReady()
  const { data: version } = useVersion()

  const lastChecked =
    ready.dataUpdatedAt > 0 ? new Date(ready.dataUpdatedAt).toLocaleTimeString() : null

  return (
    <div className="mx-auto max-w-2xl space-y-6">
      <header className="flex items-start justify-between">
        <div>
          <h1 className="text-2xl font-semibold">Backend status</h1>
          <p className="text-muted-foreground">Live readiness probe from the backend.</p>
        </div>
        <Button
          variant="outline"
          size="sm"
          onClick={() => ready.refetch()}
          disabled={ready.isFetching}
        >
          {ready.isFetching ? 'Refreshing…' : 'Refresh'}
        </Button>
      </header>

      {ready.isPending && <p className="text-muted-foreground">Loading…</p>}

      {ready.isError && (
        <Card>
          <CardContent className="pt-6">
            <p className="text-destructive">
              Backend unreachable: {(ready.error as Error).message}
            </p>
          </CardContent>
        </Card>
      )}

      {ready.data !== undefined && (
        <>
          <Card>
            <CardHeader className="pb-3">
              <CardTitle className="flex items-center justify-between text-base">
                <span>Overall</span>
                <Badge variant={ready.data.status === 'ok' ? 'ok' : 'err'}>
                  {ready.data.status === 'ok' ? 'Operational' : 'Unavailable'}
                </Badge>
              </CardTitle>
            </CardHeader>
            {(version || lastChecked) && (
              <CardContent className="text-muted-foreground space-y-1 text-xs">
                {version && (
                  <p>
                    Version: <span className="text-foreground/90 font-mono">{version.version}</span>
                    {' · '}
                    {version.environment}
                  </p>
                )}
                {lastChecked && <p>Last checked: {lastChecked}</p>}
              </CardContent>
            )}
          </Card>

          <Card>
            <CardHeader className="pb-3">
              <CardTitle className="text-base">Checks</CardTitle>
            </CardHeader>
            <CardContent className="divide-y">
              {Object.entries(ready.data.checks).map(([name, result]) => (
                <CheckRow key={name} name={name} result={result} />
              ))}
            </CardContent>
          </Card>
        </>
      )}
    </div>
  )
}
