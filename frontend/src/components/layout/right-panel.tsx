"use client"

import { useEffect, useState, useRef } from "react"
import { Card } from "@/components/ui/card"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from "@/components/ui/collapsible"
import { JSONSyntaxHighlighter } from "@/components/ui/json-syntax-highlighter"
import { ChevronDown, ChevronRight, Clock, Wrench, FileText, Database, Activity, Eye, GitBranch, Repeat } from "lucide-react"
import { LogEvent } from "@/components/log/log-event"
import { useApp } from "@/contexts/app-context"
import { useI18n } from "@/contexts/i18n-context"
import { cn } from "@/lib/utils"

interface StepExecution {
  id: string
  runtime_step_id?: string
  template_step_id?: string
  name: string
  description: string
  status: "pending" | "running" | "completed" | "failed" | "skipped"
  tool_names?: string[]
  dependencies: string[]
  runtime_dependencies?: string[]
  started_at?: string | number
  completed_at?: string | number
  result_data?: unknown
  step_data?: unknown
  file_outputs?: Array<{
  filename?: string
  file_id?: string
  file_path?: string
  relative_path?: string
  download_path?: string
}> | string[]
  conditional_branches?: Record<string, string>
  required_branch?: string | null
  is_conditional?: boolean
  step_kind?: "normal" | "map"
  map_spec?: {
    item_binding?: string
    chunk_size?: number
    collection_output?: {
      step_id?: string
      field?: string
    }
    collection_plan?: {
      steps?: PlanStepData[]
    }
    worker_plan?: {
      steps?: PlanStepData[]
    }
  } | null
  parent_map_step_id?: string | null
  worker_instance_id?: string | null
  worker_item_index?: number | null
  worker_depth?: number
  execution_scope?: string | null
}

interface PlanStepData {
  id: string
  name: string
  description?: string
  dependencies?: string[]
  tool_names?: string[]
  step_kind?: "normal" | "map"
  map_spec?: StepExecution["map_spec"]
}

interface TraceEvent {
  event_id: string
  event_type: string
  step_id?: string
  timestamp: string
  data: unknown
}

interface RightPanelProps {
  steps: StepExecution[]
  traceEvents: TraceEvent[]
  selectedStepId?: string
  onStepSelect?: (stepId: string) => void
  onPauseStep?: (stepId: string) => void
  onResumeStep?: (stepId: string) => void
  onRetryStep?: (stepId: string) => void
}

function NestedPlanView({
  title,
  steps,
  depth = 0,
}: {
  title: string
  steps?: PlanStepData[]
  depth?: number
}) {
  if (!steps || steps.length === 0) return null
  return (
    <div className="space-y-2">
      <div className="text-sm font-medium text-foreground">{title}</div>
      <div className="space-y-2">
        {steps.map((step, index) => (
          <div
            key={`${title}-${step.id}-${index}`}
            className="rounded border border-border/60 bg-muted/20 p-2 space-y-1"
            style={{ marginLeft: depth > 0 ? `${depth * 12}px` : 0 }}
          >
            <div className="flex items-center gap-2">
              <span className="text-sm font-medium">{index + 1}. {step.name}</span>
              {step.step_kind === "map" && (
                <Badge variant="secondary" className="text-[10px] gap-1">
                  <Repeat className="h-3 w-3" />
                  map
                </Badge>
              )}
            </div>
            {step.description && (
              <div className="text-xs text-muted-foreground">{step.description}</div>
            )}
            {step.dependencies && step.dependencies.length > 0 && (
              <div className="text-xs text-blue-600 dark:text-blue-400">
                Depends on: {step.dependencies.join(", ")}
              </div>
            )}
            {step.tool_names && step.tool_names.length > 0 && (
              <div className="text-xs font-mono text-muted-foreground">
                {step.tool_names.join(", ")}
              </div>
            )}
            {step.step_kind === "map" && step.map_spec && (
              <div className="rounded border border-dashed border-border/60 bg-background/50 p-2 space-y-2">
                <div className="text-xs text-muted-foreground flex flex-wrap gap-2">
                  {step.map_spec.item_binding && <span>bind: <code>{step.map_spec.item_binding}</code></span>}
                  {typeof step.map_spec.chunk_size === "number" && <span>chunk: <code>{step.map_spec.chunk_size}</code></span>}
                  {step.map_spec.collection_output?.step_id && step.map_spec.collection_output?.field && (
                    <span>source: <code>{step.map_spec.collection_output.step_id}.{step.map_spec.collection_output.field}</code></span>
                  )}
                </div>
                <NestedPlanView title="Collection Plan" steps={step.map_spec.collection_plan?.steps} depth={depth + 1} />
                <NestedPlanView title="Worker Plan" steps={step.map_spec.worker_plan?.steps} depth={depth + 1} />
              </div>
            )}
          </div>
        ))}
      </div>
    </div>
  )
}

function groupByWorkerInstance(steps: StepExecution[]) {
  const groups = new Map<string, StepExecution[]>()
  steps.forEach((step) => {
    if (!step.worker_instance_id) return
    const existing = groups.get(step.worker_instance_id) || []
    existing.push(step)
    groups.set(step.worker_instance_id, existing)
  })
  return groups
}

function buildOrderedStepIds(steps: StepExecution[]): string[] {
  const ordered: string[] = []
  const topLevel = steps.filter(step => !step.parent_map_step_id)

  const walkStep = (step: StepExecution) => {
    ordered.push(step.id)

    const directChildren = steps.filter(
      child => child.parent_map_step_id === step.id && !child.worker_instance_id
    )
    directChildren.forEach(walkStep)

    const workerSteps = steps.filter(
      child => child.parent_map_step_id === step.id && !!child.worker_instance_id
    )
    const grouped = groupByWorkerInstance(workerSteps)
    grouped.forEach((groupSteps) => {
      const directWorkerSteps = groupSteps.filter(
        groupStep => groupStep.parent_map_step_id === step.id
      )
      directWorkerSteps.forEach(walkStep)
    })
  }

  topLevel.forEach(walkStep)
  return ordered
}

function ExecutionTree({
  steps,
  selectedStepId,
  onSelect,
}: {
  steps: StepExecution[]
  selectedStepId?: string
  onSelect?: (stepId: string) => void
}) {
  const renderStepRow = (step: StepExecution, depth: number) => (
    <button
      key={step.id}
      onClick={() => onSelect?.(step.id)}
      className={cn(
        "w-full text-left rounded border px-3 py-2 transition-colors",
        selectedStepId === step.id ? "border-primary bg-primary/10" : "border-border bg-card/40 hover:bg-muted/40"
      )}
      style={{ marginLeft: `${depth * 16}px` }}
    >
      <div className="flex items-center justify-between gap-2">
        <div className="flex items-center gap-2 min-w-0">
          {step.step_kind === "map" && <Repeat className="h-3.5 w-3.5 text-muted-foreground shrink-0" />}
          <span className="text-sm font-medium truncate">{step.name}</span>
        </div>
        <Badge variant="outline" className="text-[10px] shrink-0">{step.status}</Badge>
      </div>
    </button>
  )

  const renderChildren = (parentStepId: string, depth: number): React.ReactNode[] => {
    const rows: React.ReactNode[] = []
    const directChildren = steps.filter(
      step => step.parent_map_step_id === parentStepId && !step.worker_instance_id
    )

    directChildren.forEach((step) => {
      rows.push(renderStepRow(step, depth))
      rows.push(...renderChildren(step.id, depth + 1))
    })

    const workerSteps = steps.filter(
      step => step.parent_map_step_id === parentStepId && !!step.worker_instance_id
    )
    const workerGroups = groupByWorkerInstance(workerSteps)
    workerGroups.forEach((groupSteps, workerInstanceId) => {
      const itemIndex = groupSteps[0]?.worker_item_index
      rows.push(
        <div
          key={`${workerInstanceId}-header`}
          className="rounded border border-dashed border-border/70 bg-muted/10 px-3 py-2 text-xs text-muted-foreground"
          style={{ marginLeft: `${depth * 16}px` }}
        >
          Worker {typeof itemIndex === "number" ? itemIndex + 1 : workerInstanceId}
        </div>
      )
      const directWorkerSteps = groupSteps.filter(
        step => step.parent_map_step_id === parentStepId
      )
      directWorkerSteps.forEach((step) => {
        rows.push(renderStepRow(step, depth + 1))
        rows.push(...renderChildren(step.id, depth + 2))
      })
    })

    return rows
  }

  const topLevelSteps = steps.filter(step => !step.parent_map_step_id)

  return (
    <Card className="bg-card/50 border-border">
      <div className="p-4 space-y-2">
        <div className="text-base font-medium text-foreground">Execution Tree</div>
        {topLevelSteps.map((step) => (
          <div key={`tree-${step.id}`} className="space-y-2">
            {renderStepRow(step, 0)}
            {renderChildren(step.id, 1)}
          </div>
        ))}
      </div>
    </Card>
  )
}

// Step detail component with file preview functionality
function StepDetail({ step }: { step: StepExecution }) {
  const { openFilePreview } = useApp()
  const { t } = useI18n()
  const [expandedSections, setExpandedSections] = useState({
    resultData: false,
    stepData: false,
    fileOutputs: true, // Default to expanded for better file access
  })

  const toggleSection = (section: string) => {
    setExpandedSections(prev => ({
      ...prev,
      [section]: !prev[section as keyof typeof prev]
    }))
  }

  const handlePreviewFile = (fileId: string, fileName: string, allFiles?: any[]) => {
    // Convert all files to the format expected by openFilePreview
    const files = (allFiles?.map(file => {
      if (typeof file === 'object' && file !== null && file.file_id) {
        return {
          filePath: file.file_id,
          fileName: file.filename || 'Unknown File',
        }
      }
      return null
    }).filter(Boolean) as { fileId: string; fileName: string }[] | undefined) || [{
      filePath: fileId,
      fileName: fileName || fileId,
    }]

    openFilePreview(
      fileId,
      fileName || fileId,
      files as { fileId: string; fileName: string }[],
      0
    )
  }

  const handlePreviewAllFiles = () => {
    if (step.file_outputs && step.file_outputs.length > 0) {
      const files = step.file_outputs.map(file => ({
        fileId: typeof file === 'object' && file !== null ? (file.file_id || '') : '',
        fileName: typeof file === 'object' && file !== null ? (file.filename || 'Unknown File') : 'Unknown File'
      })).filter(file => !!file.fileId)

      if (files[0].fileId) {
        openFilePreview(
          files[0].fileId as string,
          files[0].fileName as string,
          files as { fileId: string; fileName: string }[],
          0
        )
      }
    }
  }

  const formatDuration = () => {
    if (!step.started_at) return t("agent.layout.common.notStarted")
    if (!step.completed_at) return t("agent.layout.common.inProgress")
    try {
      const start = new Date(step.started_at).getTime()
      const end = new Date(step.completed_at).getTime()
      const duration = end - start
      if (duration < 1000) return `${duration}ms`
      if (duration < 60000) return `${(duration / 1000).toFixed(1)}s`
      return `${(duration / 60000).toFixed(1)}min`
    } catch {
      return t("agent.layout.common.unknown")
    }
  }

  return (
    <div className="space-y-4">
      {/* Result Data */}
      {step.result_data !== undefined && (
        <Collapsible open={expandedSections.resultData} onOpenChange={() => toggleSection('resultData')}>
          <Card className="border-border">
            <CollapsibleTrigger asChild>
              <div className="flex items-center justify-between p-3 cursor-pointer hover:bg-muted/50 transition-colors">
                <div className="flex items-center gap-2">
                  <Database className="h-4 w-4 text-muted-foreground" />
                  <span className="text-base font-medium">{t("agent.layout.right.labels.resultData")}</span>
                </div>
                {expandedSections.resultData ? (
                  <ChevronDown className="h-4 w-4 text-muted-foreground" />
                ) : (
                  <ChevronRight className="h-4 w-4 text-muted-foreground" />
                )}
              </div>
            </CollapsibleTrigger>
            <CollapsibleContent>
              <div className="px-3 pb-3">
                <JSONSyntaxHighlighter data={step.result_data} />
              </div>
            </CollapsibleContent>
          </Card>
        </Collapsible>
      )}

      {/* Step Data */}
      {step.step_data !== undefined && (
        <Collapsible open={expandedSections.stepData} onOpenChange={() => toggleSection('stepData')}>
          <Card className="border-border">
            <CollapsibleTrigger asChild>
              <div className="flex items-center justify-between p-3 cursor-pointer hover:bg-muted/50 transition-colors">
                <div className="flex items-center gap-2">
                  <Activity className="h-4 w-4 text-muted-foreground" />
                  <span className="text-base font-medium">{t("agent.layout.right.labels.stepData")}</span>
                </div>
                {expandedSections.stepData ? (
                  <ChevronDown className="h-4 w-4 text-muted-foreground" />
                ) : (
                  <ChevronRight className="h-4 w-4 text-muted-foreground" />
                )}
              </div>
            </CollapsibleTrigger>
            <CollapsibleContent>
              <div className="px-3 pb-3">
                <JSONSyntaxHighlighter data={step.step_data} />
              </div>
            </CollapsibleContent>
          </Card>
        </Collapsible>
      )}

      {/* File Outputs */}
      {step.file_outputs && step.file_outputs.length > 0 && (
        <Collapsible open={expandedSections.fileOutputs} onOpenChange={() => toggleSection('fileOutputs')}>
          <Card className="border-border">
            <CollapsibleTrigger asChild>
              <div className="flex items-center justify-between p-3 cursor-pointer hover:bg-muted/50 transition-colors">
                <div className="flex items-center gap-2">
                  <FileText className="h-4 w-4 text-muted-foreground" />
                  <span className="text-base font-medium">{t("agent.layout.right.labels.fileOutputs")} ({step.file_outputs.length})</span>
                </div>
                <div className="flex items-center gap-2">
                  {/* Preview All Files Button */}
                  <Button
                    variant="outline"
                    size="sm"
                    onClick={handlePreviewAllFiles}
                    className="h-7 px-2 text-xs"
                    title={t("agent.layout.right.tooltips.previewAllFiles")}
                  >
                    <Eye className="h-3 w-3 mr-1" />
                    {t("agent.layout.right.buttons.previewAll")}
                  </Button>
                  {expandedSections.fileOutputs ? (
                    <ChevronDown className="h-4 w-4 text-muted-foreground" />
                  ) : (
                    <ChevronRight className="h-4 w-4 text-muted-foreground" />
                  )}
                </div>
              </div>
            </CollapsibleTrigger>
            <CollapsibleContent>
              <div className="px-3 pb-3 space-y-2">
                {step.file_outputs.map((file, index) => {
                  const fileName = typeof file === 'object' && file !== null ? (file.filename || 'Unknown File') : 'Unknown File'
                  const fileId = typeof file === 'object' && file !== null ? (file.file_id || '') : ''
                  return (
                    <div key={index} className="flex items-center justify-between p-2 bg-muted/30 rounded hover:bg-muted/50 transition-colors">
                      <div className="flex items-center gap-2 flex-1 min-w-0">
                        <FileText className="h-3 w-3 text-muted-foreground flex-shrink-0" />
                        <span className="text-sm text-muted-foreground font-mono truncate" title={fileId}>
                          {fileName}
                        </span>
                      </div>
                      <div className="flex items-center gap-1 flex-shrink-0">
                        <Button
                          variant="ghost"
                          size="sm"
                          onClick={() => fileId && handlePreviewFile(fileId, fileName || 'Unknown File', step.file_outputs)}
                          disabled={!fileId}
                          className="h-6 w-6 p-0"
                          title={t("agent.layout.right.tooltips.previewFile")}
                        >
                          <Eye className="h-3 w-3" />
                        </Button>
                      </div>
                    </div>
                  )
                })}
              </div>
            </CollapsibleContent>
          </Card>
        </Collapsible>
      )}

      {/* Execution Time */}
      <div className="flex items-center justify-between p-3 bg-muted/30 rounded">
        <div className="flex items-center gap-2">
          <Clock className="h-4 w-4 text-muted-foreground" />
          <span className="text-sm text-muted-foreground">{t("agent.layout.right.labels.executionTime")}</span>
        </div>
        <span className="text-sm font-medium">{formatDuration()}</span>
      </div>
    </div>
  )
}

// Step summary component
function StepSummary({ step }: { step: StepExecution }) {
  const { t } = useI18n()
  const formatDuration = () => {
    if (!step.started_at) return t("agent.layout.common.notStarted")
    if (!step.completed_at) return t("agent.layout.common.inProgress")
    try {
      const start = new Date(step.started_at).getTime()
      const end = new Date(step.completed_at).getTime()
      const duration = end - start
      if (duration < 1000) return `${duration}ms`
      if (duration < 60000) return `${(duration / 1000).toFixed(1)}s`
      return `${(duration / 60000).toFixed(1)}min`
    } catch {
      return t("agent.layout.common.unknown")
    }
  }

  const getStatusColor = (status: string) => {
    const colors = {
      pending: "text-muted-foreground",
      running: "text-primary",
      completed: "text-green-500",
      failed: "text-destructive",
      skipped: "text-gray-400",
    }
    return colors[status as keyof typeof colors] || colors.pending
  }

  const getStatusText = (status: string) => {
    const texts: Record<string, string> = {
      pending: t("agent.layout.status.pending"),
      running: t("agent.layout.status.running"),
      completed: t("agent.layout.status.completed"),
      failed: t("agent.layout.status.failed"),
      skipped: t("agent.layout.status.skipped"),
    }
    return texts[status] || status
  }

  return (
    <div className="space-y-3">
      {/* Basic Info */}
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2">
          <span className="text-base font-medium text-foreground">{step.name}</span>
          {step.step_kind === "map" && (
            <Badge variant="secondary" className="gap-1">
              <Repeat className="h-3 w-3" />
              map
            </Badge>
          )}
        </div>
        <Badge variant="outline" className={`text-sm ${getStatusColor(step.status)}`}>
          {getStatusText(step.status)}
        </Badge>
      </div>

      {/* Conditional Branch Indicator */}
      {step.is_conditional && step.conditional_branches && Object.keys(step.conditional_branches).length > 0 && (
        <div className="flex items-start gap-2 p-2 bg-purple-500/10 border border-purple-500/20 rounded">
          <GitBranch className="h-4 w-4 text-purple-400 mt-0.5 flex-shrink-0" />
          <div className="flex-1">
            <div className="text-sm font-medium text-purple-400 mb-1">{t("agent.layout.right.branch.conditionalNode")}</div>
            <div className="text-xs text-muted-foreground">
              {t("agent.layout.right.branch.optionalBranches")}: {Object.keys(step.conditional_branches).join(", ")}
            </div>
          </div>
        </div>
      )}

      {/* Required Branch Indicator */}
      {step.required_branch && (
        <div className="flex items-start gap-2 p-2 bg-blue-500/10 border border-blue-500/20 rounded">
          <GitBranch className="h-4 w-4 text-blue-400 mt-0.5 flex-shrink-0" />
          <div className="flex-1">
            <div className="text-sm font-medium text-blue-400 mb-1">{t("agent.layout.right.branch.branchCondition")}</div>
            <div className="text-xs text-muted-foreground">
              {t("agent.layout.right.branch.requiredBranch")}: <code className="bg-blue-500/20 px-1 py-0.5 rounded">{step.required_branch}</code>
            </div>
          </div>
        </div>
      )}

      {/* Description */}
      {step.description && (
        <div className="flex items-start gap-2">
          <FileText className="h-4 w-4 text-muted-foreground mt-0.5 flex-shrink-0" />
          <span className="text-base text-muted-foreground leading-relaxed">{step.description}</span>
        </div>
      )}

      {/* Tools */}
      {step.tool_names && step.tool_names.length > 0 && (
        <div className="flex items-center gap-2">
          <Wrench className="h-4 w-4 text-muted-foreground flex-shrink-0" />
          <span className="text-base font-mono text-muted-foreground bg-muted px-2 py-1 rounded">
            {step.tool_names.join(", ")}
          </span>
        </div>
      )}

      {step.step_kind === "map" && step.map_spec && (
        <div className="space-y-3 rounded border border-dashed border-border/70 p-3 bg-muted/10">
          <div className="flex items-start gap-2">
            <Repeat className="h-4 w-4 text-muted-foreground mt-0.5 flex-shrink-0" />
            <div className="text-sm text-muted-foreground space-y-1">
              {step.map_spec.item_binding && (
                <div>Binding: <code className="bg-muted px-1 py-0.5 rounded">{step.map_spec.item_binding}</code></div>
              )}
              {typeof step.map_spec.chunk_size === "number" && (
                <div>Chunk size: <code className="bg-muted px-1 py-0.5 rounded">{step.map_spec.chunk_size}</code></div>
              )}
              {step.map_spec.collection_output?.step_id && step.map_spec.collection_output?.field && (
                <div>
                  Collection source:{" "}
                  <code className="bg-muted px-1 py-0.5 rounded">
                    {step.map_spec.collection_output.step_id}.{step.map_spec.collection_output.field}
                  </code>
                </div>
              )}
            </div>
          </div>
          <NestedPlanView title="Collection Plan" steps={step.map_spec.collection_plan?.steps} />
          <NestedPlanView title="Worker Plan" steps={step.map_spec.worker_plan?.steps} />
        </div>
      )}

      {/* Time Information */}
      {(step.started_at || step.completed_at) && (
        <div className="flex items-center gap-2">
          <Clock className="h-4 w-4 text-muted-foreground flex-shrink-0" />
          <div className="text-base text-muted-foreground">
            {formatDuration()}
          </div>
        </div>
      )}

      {/* Data Summary */}
      <div className="grid grid-cols-2 gap-2">
        {step.result_data !== undefined && (
          <div className="flex items-center gap-1">
            <Database className="h-3 w-3 text-muted-foreground" />
            <span className="text-sm text-muted-foreground">{t("agent.layout.right.labels.resultData")}</span>
          </div>
        )}
        {step.step_data !== undefined && (
          <div className="flex items-center gap-1">
            <Activity className="h-3 w-3 text-muted-foreground" />
            <span className="text-sm text-muted-foreground">{t("agent.layout.right.labels.stepData")}</span>
          </div>
        )}
        {step.file_outputs && step.file_outputs.length > 0 && (
          <div className="flex items-center gap-1">
            <FileText className="h-3 w-3 text-muted-foreground" />
            <span className="text-sm text-muted-foreground">{t("agent.layout.right.counts.files", { count: String(step.file_outputs.length) })}</span>
          </div>
        )}
      </div>
    </div>
  )
}


export function RightPanel({
  steps,
  traceEvents,
  selectedStepId,
  onStepSelect,
  onPauseStep,
  onResumeStep,
  onRetryStep,
}: RightPanelProps) {
  const { t } = useI18n()
  const selectedStep = steps.find(step => step.id === selectedStepId)
  const orderedStepIds = buildOrderedStepIds(steps)
  const [isTransitioning, setIsTransitioning] = useState(false)
  const scrollContainerRef = useRef<HTMLDivElement>(null)
  const prevTraceEventsCountRef = useRef(traceEvents.length)
  const userManuallySelectedRef = useRef(false)
  const isUserAtBottomRef = useRef(true)


  // Auto-select the first step if none is selected
  useEffect(() => {
    if (steps.length > 0 && !selectedStepId) {
      onStepSelect?.(orderedStepIds[0] || steps[0].id)
    }
  }, [steps, orderedStepIds, selectedStepId, onStepSelect])

  // Auto-scroll to running step when step status changes
  useEffect(() => {
    // Don't auto-track if user manually selected a step
    if (userManuallySelectedRef.current) {
      return
    }

    if (steps.length > 0) {
      const runningStep = steps.find(step => step.status === 'running')

      if (runningStep && runningStep.id !== selectedStepId) {
        // Found a running step that's not currently selected
        setIsTransitioning(true)
        setTimeout(() => {
          onStepSelect?.(runningStep.id)
          setTimeout(() => setIsTransitioning(false), 50)
        }, 100)
      } else if (!runningStep) {
        // No running step, find the most recently completed step
        const completedSteps = steps.filter(step => step.status === 'completed')
        if (completedSteps.length > 0) {
          const latestCompletedStep = completedSteps[completedSteps.length - 1]
          if (latestCompletedStep.id !== selectedStepId) {
            setIsTransitioning(true)
            setTimeout(() => {
              onStepSelect?.(latestCompletedStep.id)
              setTimeout(() => setIsTransitioning(false), 50)
            }, 100)
          }
        }
      }
    }
  }, [steps, selectedStepId, onStepSelect])

  // Reset manual selection when new running step appears (indicating new execution)
  useEffect(() => {
    const hasRunningStep = steps.some(step => step.status === 'running')
    if (hasRunningStep) {
      userManuallySelectedRef.current = false
    }
  }, [steps])

  // Track whether user has scrolled to bottom; only auto-scroll when at bottom
  useEffect(() => {
    const el = scrollContainerRef.current
    if (!el) return

    const updateIsAtBottom = () => {
      const threshold = 24 // px tolerance
      const atBottom = el.scrollTop + el.clientHeight >= el.scrollHeight - threshold
      isUserAtBottomRef.current = atBottom
    }

    // Initialize and listen
    updateIsAtBottom()
    el.addEventListener('scroll', updateIsAtBottom)

    return () => {
      el.removeEventListener('scroll', updateIsAtBottom)
    }
  }, [])

  // Auto-scroll to bottom when new trace events are added or step changes, but only if user is at bottom
  useEffect(() => {
    const scrollToBottom = () => {
      if (scrollContainerRef.current) {
        // Try scrollTo method first
        scrollContainerRef.current.scrollTo({
          top: scrollContainerRef.current.scrollHeight,
          behavior: 'smooth'
        })

        // Fallback: scroll the last element into view
        const lastElement = scrollContainerRef.current.lastElementChild
        if (lastElement) {
          lastElement.scrollIntoView({ behavior: 'smooth', block: 'end' })
        }
      }
    }

    const shouldAutoScroll = isUserAtBottomRef.current

    // Scroll when trace events are added
    if (scrollContainerRef.current && traceEvents.length > prevTraceEventsCountRef.current && shouldAutoScroll) {
      setTimeout(scrollToBottom, 100)
      setTimeout(scrollToBottom, 200)
      setTimeout(scrollToBottom, 300)
    }

    // Also scroll when selected step changes (to show the latest logs for that step)
    if (selectedStepId && shouldAutoScroll) {
      setTimeout(scrollToBottom, 50)
      setTimeout(scrollToBottom, 150)
    }

    prevTraceEventsCountRef.current = traceEvents.length
  }, [traceEvents, selectedStepId])

  const stepEvents = traceEvents.filter(event => event.step_id === selectedStepId)

  return (
    <div className="flex flex-col h-full bg-card/30 min-w-[400px]">
      {/* Header */}
      <div className="p-4 border-b border-border bg-card/50 backdrop-blur-sm">
        <div className="flex items-center justify-between mb-3">
          <h2 className="text-xl font-semibold text-foreground">{t("agent.layout.right.titles.stepDetail")}</h2>
        </div>

        {/* Step switcher */}
        <div className="flex items-center justify-between">
          <span className="text-sm text-muted-foreground">{t("agent.layout.right.labels.currentStep")}</span>
          <div className="flex items-center gap-2">
            <button
              onClick={() => {
                const currentIndex = orderedStepIds.findIndex(id => id === selectedStepId)
                if (currentIndex > 0) {
                  userManuallySelectedRef.current = true
                  setIsTransitioning(true)
                  setTimeout(() => {
                    onStepSelect?.(orderedStepIds[currentIndex - 1])
                    setTimeout(() => setIsTransitioning(false), 50)
                  }, 100)
                }
              }}
              disabled={!selectedStepId || orderedStepIds.findIndex(id => id === selectedStepId) === 0 || isTransitioning}
              className="p-1 text-sm bg-muted border border-border rounded hover:bg-accent disabled:opacity-50 disabled:cursor-not-allowed transition-opacity"
            >
              ← {t("agent.layout.right.buttons.prevStep")}
            </button>
            <span className="text-sm font-medium min-w-[120px] text-center">
              {selectedStepId ? steps.find(s => s.id === selectedStepId)?.name : t("agent.layout.right.labels.none")}
            </span>
            <button
              onClick={() => {
                const currentIndex = orderedStepIds.findIndex(id => id === selectedStepId)
                if (currentIndex < orderedStepIds.length - 1) {
                  userManuallySelectedRef.current = true
                  setIsTransitioning(true)
                  setTimeout(() => {
                    onStepSelect?.(orderedStepIds[currentIndex + 1])
                    setTimeout(() => setIsTransitioning(false), 50)
                  }, 100)
                }
              }}
              disabled={!selectedStepId || orderedStepIds.findIndex(id => id === selectedStepId) === orderedStepIds.length - 1 || isTransitioning}
              className="p-1 text-sm bg-muted border border-border rounded hover:bg-accent disabled:opacity-50 disabled:cursor-not-allowed transition-opacity"
            >
              {t("agent.layout.right.buttons.nextStep")} →
            </button>
          </div>
        </div>
      </div>

      {/* Main Content */}
      <div className="flex-1 flex flex-col w-full overflow-hidden">
        <div
          ref={scrollContainerRef}
          className={`flex-1 overflow-y-auto transition-opacity duration-200 ${isTransitioning ? 'opacity-50' : 'opacity-100'}`}
        >
          <div className="p-4 space-y-4">
            <ExecutionTree
              steps={steps}
              selectedStepId={selectedStepId}
              onSelect={(stepId) => {
                userManuallySelectedRef.current = true
                onStepSelect?.(stepId)
              }}
            />

            {/* Step Summary */}
            {selectedStep && (
              <Card className="bg-card/50 border-border">
                <div className="p-4">
                  <StepSummary step={selectedStep} />
                </div>
              </Card>
            )}

            {/* Step Details */}
            {selectedStep && (
              <StepDetail step={selectedStep} />
            )}

            {/* Trace Events */}
            {stepEvents.length > 0 && (
              <Card className="bg-card/50 border-border">
                <div className="p-4">
                  <div className="flex items-center justify-between mb-3">
                    <h3 className="text-base font-medium text-foreground">{t("agent.layout.right.titles.executionLogs")}</h3>
                    <Badge variant="outline" className="text-sm border-border">
                      {t("agent.layout.right.counts.logs", { count: String(stepEvents.length) })}
                    </Badge>
                  </div>
                  <div className="space-y-2">
                    {stepEvents.map((event, index) => (
                      <LogEvent key={event.event_id || `event-${index}`} event={event} />
                    ))}
                  </div>
                </div>
              </Card>
            )}

            {/* Empty State */}
            {!selectedStep && (
              <div className="text-center text-muted-foreground py-8">
                {t("agent.layout.right.empty.selectStepHint")}
              </div>
            )}

            {selectedStep && stepEvents.length === 0 && (
              <div className="text-center text-muted-foreground py-4">
                {t("agent.layout.right.empty.noLogs")}
              </div>
            )}
          </div>
        </div>
      </div>
    </div>
  )
}
