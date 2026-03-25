"use client"

import { useState } from "react"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import { Button } from "@/components/ui/button"
import { Badge } from "@/components/ui/badge"
import { Separator } from "@/components/ui/separator"
import { ChevronDown, ChevronRight, Brain, Search, Target, Info, Repeat } from "lucide-react"
import { cn } from "@/lib/utils"
import { useI18n } from "@/contexts/i18n-context"

interface CollapsibleSectionProps {
  title: string
  icon?: React.ReactNode
  defaultExpanded?: boolean
  children: React.ReactNode
  badge?: string
}

interface PlanMemoryDetailsProps {
  planData: {
    goal?: string
    steps?: PlanStepData[]
    enhancedGoal?: string
    memories?: Array<{
      content: string
      category?: string
    }>
  }
  memoriesFound?: number
  memoriesUsed?: number
  memoryCategory?: string
}

interface PlanData {
  id?: string
  goal?: string
  task_name?: string
  steps?: PlanStepData[]
}

interface PlanStepData {
  id: string
  name: string
  description?: string
  tool_names?: string[]
  dependencies?: string[]
  step_kind?: "normal" | "map"
  map_spec?: {
    item_binding?: string
    chunk_size?: number
    collection_plan?: PlanData
    worker_plan?: PlanData
    collection_output?: {
      step_id?: string
      field?: string
    }
  } | null
}

function PlanStepCard({
  step,
  index,
  depth = 0,
}: {
  step: PlanStepData
  index: number
  depth?: number
}) {
  const { t } = useI18n()
  const isMap = step.step_kind === "map"

  return (
    <div
      className="text-xs p-2 bg-muted/20 rounded border border-border/50 space-y-1"
      style={{ marginLeft: depth > 0 ? `${depth * 12}px` : 0 }}
    >
      <div className="flex items-center justify-between gap-2">
        <span className="font-medium">
          {index + 1}. {step.name}
        </span>
        <div className="flex items-center gap-1">
          {isMap && (
            <Badge variant="secondary" className="text-[10px] gap-1">
              <Repeat className="h-3 w-3" />
              map
            </Badge>
          )}
          {step.tool_names && step.tool_names.length > 0 && (
            <Badge variant="outline" className="text-xs">
              {step.tool_names.join(", ")}
            </Badge>
          )}
        </div>
      </div>
      {step.description && (
        <div className="text-muted-foreground">{step.description}</div>
      )}
      {step.dependencies && step.dependencies.length > 0 && (
        <div className="text-xs text-blue-600 dark:text-blue-400">
          {t('agent.planDetails.plan.dependenciesPrefix')}{step.dependencies.join(", ")}
        </div>
      )}
      {isMap && step.map_spec && (
        <div className="mt-2 space-y-2 rounded border border-dashed border-border/70 p-2 bg-background/40">
          <div className="flex flex-wrap gap-2 text-[11px] text-muted-foreground">
            {step.map_spec.item_binding && <span>bind: <code>{step.map_spec.item_binding}</code></span>}
            {typeof step.map_spec.chunk_size === "number" && <span>chunk: <code>{step.map_spec.chunk_size}</code></span>}
            {step.map_spec.collection_output?.step_id && step.map_spec.collection_output?.field && (
              <span>
                source: <code>{step.map_spec.collection_output.step_id}.{step.map_spec.collection_output.field}</code>
              </span>
            )}
          </div>
          {step.map_spec.collection_plan?.steps && step.map_spec.collection_plan.steps.length > 0 && (
            <div className="space-y-1">
              <div className="text-[11px] font-medium text-muted-foreground">Collection Plan</div>
              {step.map_spec.collection_plan.steps.map((nestedStep, nestedIndex) => (
                <PlanStepCard
                  key={`${step.id}-collection-${nestedStep.id}-${nestedIndex}`}
                  step={nestedStep}
                  index={nestedIndex}
                  depth={depth + 1}
                />
              ))}
            </div>
          )}
          {step.map_spec.worker_plan?.steps && step.map_spec.worker_plan.steps.length > 0 && (
            <div className="space-y-1">
              <div className="text-[11px] font-medium text-muted-foreground">Worker Plan</div>
              {step.map_spec.worker_plan.steps.map((nestedStep, nestedIndex) => (
                <PlanStepCard
                  key={`${step.id}-worker-${nestedStep.id}-${nestedIndex}`}
                  step={nestedStep}
                  index={nestedIndex}
                  depth={depth + 1}
                />
              ))}
            </div>
          )}
        </div>
      )}
    </div>
  )
}

// Common collapsible component
export function CollapsibleSection({
  title,
  icon,
  defaultExpanded = false,
  children,
  badge
}: CollapsibleSectionProps) {
  const [isExpanded, setIsExpanded] = useState(defaultExpanded)

  return (
    <div className="mt-0.5">
      <Button
        variant="ghost"
        size="sm"
        className="w-full justify-between p-0.5 h-5 hover:bg-muted/20 rounded"
        onClick={() => setIsExpanded(!isExpanded)}
      >
        <div className="flex items-center gap-0.5">
          {isExpanded ? (
            <ChevronDown className="h-3 w-3" />
          ) : (
            <ChevronRight className="h-3 w-3" />
          )}
          {icon && <span className="h-3 w-3">{icon}</span>}
          <span className="text-xs font-medium">{title}</span>
          {badge && (
            <Badge variant="outline" className="text-xs px-1 py-0">
              {badge}
            </Badge>
          )}
        </div>
      </Button>

      {isExpanded && (
        <div className="mt-1">
          {children}
        </div>
      )}
    </div>
  )
}

export function PlanMemoryDetails({
  planData,
  memoriesFound = 0,
  memoriesUsed = 0,
  memoryCategory,
}: PlanMemoryDetailsProps) {
  const { t } = useI18n()

  const hasMemoryInfo = memoriesFound > 0 || planData.enhancedGoal || planData.memories
  const hasDetailedPlan = planData.steps && planData.steps.length > 0

  if (!hasMemoryInfo && !hasDetailedPlan) {
    return null
  }

  return (
    <CollapsibleSection
      title={t('agent.planDetails.collapsibleTitle')}
      badge={[
        hasMemoryInfo && t('agent.planDetails.badge.memory'),
        hasDetailedPlan && t('agent.planDetails.badge.plan')
      ].filter(Boolean).join(" + ")}
    >

      <div className="space-y-4">
        {/* Memory Information */}
        {hasMemoryInfo && (
          <div className="space-y-3">
            <div className="flex items-center gap-2">
              <Brain className="h-4 w-4 text-blue-500" />
              <h4 className="text-sm font-medium">{t('agent.planDetails.memory.title')}</h4>
            </div>

            {/* Memory Retrieval Stats */}
            {(memoriesFound > 0 || memoriesUsed > 0) && (
              <div className="grid grid-cols-2 gap-2 text-xs">
                <div className="flex items-center gap-1 p-2 bg-muted/30 rounded">
                  <Search className="h-3 w-3" />
                  <span>{t('agent.planDetails.memory.stats.found', { count: memoriesFound })}</span>
                </div>
                <div className="flex items-center gap-1 p-2 bg-muted/30 rounded">
                  <Target className="h-3 w-3" />
                  <span>{t('agent.planDetails.memory.stats.used', { count: memoriesUsed })}</span>
                </div>
              </div>
            )}

            {/* Enhanced Goal */}
            {planData.enhancedGoal && (
              <div className="space-y-2">
                <div className="text-xs font-medium text-muted-foreground">{t('agent.planDetails.memory.enhancedGoalTitle')}</div>
                <div className="text-xs bg-blue-500/10 p-2 rounded border border-blue-500/20">
                  {planData.enhancedGoal}
                </div>
              </div>
            )}

            {/* Memory Details */}
            {planData.memories && planData.memories.length > 0 && (
              <div className="space-y-2">
                <div className="text-xs font-medium text-muted-foreground">{t('agent.planDetails.memory.relatedTitle')}</div>
                <div className="space-y-1">
                  {planData.memories.map((memory, index) => (
                    <div
                      key={index}
                      className="text-xs p-2 bg-muted/20 rounded border border-border/50"
                    >
                      <div className="flex items-start gap-1">
                        <Info className="h-3 w-3 mt-0.5 text-blue-400 flex-shrink-0" />
                        <span className="whitespace-pre-wrap">{memory.content}</span>
                      </div>
                      {memory.category && (
                        <Badge variant="outline" className="text-xs mt-1">
                          {memory.category || t('agent.planDetails.memory.unknownCategory')}
                        </Badge>
                      )}
                    </div>
                  ))}
                </div>
              </div>
            )}
          </div>
        )}

        {/* Plan Details */}
        {hasDetailedPlan && (
          <>
            <Separator />
            <div className="space-y-3">
              <div className="flex items-center gap-2">
                <Target className="h-4 w-4 text-green-500" />
                <h4 className="text-sm font-medium">{t('agent.planDetails.plan.title')}</h4>
              </div>

              {planData.goal && (
                <div className="space-y-1">
                  <div className="text-xs font-medium text-muted-foreground">{t('agent.planDetails.plan.goalTitle')}</div>
                  <div className="text-sm font-medium">{planData.goal}</div>
                </div>
              )}

              <div className="space-y-2">
                <div className="text-xs font-medium text-muted-foreground">
                  {t('agent.planDetails.plan.stepsTitle', { count: planData.steps?.length || 0 })}
                </div>
                <div className="space-y-1">
                  {planData.steps?.map((step, index) => (
                    <PlanStepCard key={step.id} step={step} index={index} />
                  ))}
                </div>
              </div>
            </div>
          </>
        )}
      </div>
    </CollapsibleSection>
  )
}
