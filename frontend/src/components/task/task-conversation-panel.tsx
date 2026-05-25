"use client"

import React, { useCallback, useEffect, useMemo, useRef, useState } from "react"
import { FolderOpen, GitMerge, Loader2 } from "lucide-react"
import dagre from "dagre"
import { ChatInput } from "@/components/chat/ChatInput"
import { ChatMessage } from "@/components/chat/ChatMessage"
import { TokenUsageDisplay } from "@/components/chat/TokenUsageDisplay"
import { FilePreviewActionButtons } from "@/components/file/file-preview-action-buttons"
import { FilePreviewContent } from "@/components/file/file-preview-content"
import { TaskFileManager } from "@/components/file/task-file-manager"
import { CenterPanel } from "@/components/layout/center-panel"
import { PreviewSheet } from "@/components/preview-sheet"
import { Button } from "@/components/ui/button"
import { useApp } from "@/contexts/app-context-chat"
import { useI18n } from "@/contexts/i18n-context"
import { apiRequest } from "@/lib/api-wrapper"
import { cn, getApiUrl } from "@/lib/utils"

export type TaskConversationPanelMode = "page" | "embedded-preview"

interface TaskConversationPanelProps {
  mode: TaskConversationPanelMode
  className?: string
  showTaskActions?: boolean
  showTokenUsage?: boolean
  showDagPreview?: boolean
  showTaskFiles?: boolean
  autoFocusInput?: boolean
  onSend?: (message: string, config?: any, files?: File[]) => Promise<void> | void
}

type CombinedItem = {
  id: string
  role: "user" | "assistant"
  content: string | React.ReactNode
  rawContent?: string
  timestamp: number
  traceEvents?: any[]
  interactions?: any[]
}

const toTimestampMs = (timestamp: unknown): number => {
  if (typeof timestamp === "number") {
    return timestamp < 100000000000 ? timestamp * 1000 : timestamp
  }

  const numeric = Number(timestamp)
  if (!Number.isNaN(numeric)) {
    return numeric < 100000000000 ? numeric * 1000 : numeric
  }

  return new Date(String(timestamp)).getTime()
}

const findWaitingPrompt = (currentTask: any, traceEvents: any[]) => {
  if (currentTask?.status !== "waiting_for_user") {
    return null
  }
  if (currentTask.waitingQuestion) {
    return currentTask.waitingQuestion
  }

  for (let i = traceEvents.length - 1; i >= 0; i--) {
    const event = traceEvents[i]
    if (event.event_type === "agent_message") {
      const expectsResponse = event.data?.expect_response === true || event.data?.message_type === "question"
      const message = event.data?.message || event.data?.content
      if (expectsResponse && typeof message === "string" && message.trim()) {
        return message
      }
    }
    if (event.event_type === "react_task_end") {
      const result = event.data?.result
      if (result?.status === "waiting_for_user" && typeof result.message === "string" && result.message.trim()) {
        return result.message
      }
    }
  }

  return null
}

const findWaitingInteractions = (currentTask: any, traceEvents: any[]) => {
  if (currentTask?.status !== "waiting_for_user") {
    return undefined
  }
  if (currentTask.waitingInteractions?.length) {
    return currentTask.waitingInteractions
  }

  for (let i = traceEvents.length - 1; i >= 0; i--) {
    const event = traceEvents[i]
    if (event.event_type === "agent_message") {
      const expectsResponse = event.data?.expect_response === true || event.data?.message_type === "question"
      const interactions = event.data?.metadata?.interactions
      if (expectsResponse && Array.isArray(interactions) && interactions.length > 0) {
        return interactions
      }
    }
    if (event.event_type === "react_task_end") {
      const interactions = event.data?.result?.interactions
      if (Array.isArray(interactions) && interactions.length > 0) {
        return interactions
      }
    }
  }

  return undefined
}

export function TaskConversationPanel({
  mode,
  className,
  showTaskActions = mode === "page",
  showTokenUsage = mode === "page",
  showDagPreview = mode === "page",
  showTaskFiles = mode === "page",
  autoFocusInput = mode === "page",
  onSend,
}: TaskConversationPanelProps) {
  const { state, sendMessage, pauseTask, resumeTask, openFilePreview, closeFilePreview, requestStatus, dispatch } = useApp()
  const { t } = useI18n()
  const [files, setFiles] = useState<File[]>([])
  const [dagPreviewOpen, setDagPreviewOpen] = useState(false)
  const [dagLayout, setDagLayout] = useState<"TB" | "LR">("TB")
  const [leftWidth, setLeftWidth] = useState(50)
  const [isDragging, setIsDragging] = useState(false)
  const messagesEndRef = useRef<HTMLDivElement>(null)
  const containerRef = useRef<HTMLDivElement>(null)
  const anyPreviewOpen = mode === "page" && (state.filePreview.isOpen || dagPreviewOpen)

  const handleSend = async (message: string, config?: any, filesToSend?: File[]) => {
    await (onSend ?? sendMessage)(message, config, filesToSend || files)
    setFiles([])
  }

  const combinedItems = useMemo<CombinedItem[]>(() => {
    return state.messages
      .filter((message: any) => message.role === "user" || message.isResult)
      .map((message: any) => ({
        id: message.id || `${message.role}-${toTimestampMs(message.timestamp)}`,
        role: message.role,
        content: message.content,
        rawContent: message.rawContent,
        timestamp: toTimestampMs(message.timestamp),
        traceEvents: message.traceEvents,
        interactions: message.interactions,
      }))
      .sort((a, b) => a.timestamp - b.timestamp)
  }, [state.messages])

  const waitingPrompt = useMemo(
    () => findWaitingPrompt(state.currentTask, state.traceEvents as any[]),
    [state.currentTask, state.traceEvents]
  )
  const waitingInteractions = useMemo(
    () => findWaitingInteractions(state.currentTask, state.traceEvents as any[]),
    [state.currentTask, state.traceEvents]
  )

  const activeWaitingMessageId = useMemo(() => {
    if (state.currentTask?.status !== "waiting_for_user") {
      return null
    }

    if (waitingPrompt) {
      const normalizedPrompt = waitingPrompt.trim()
      for (let i = combinedItems.length - 1; i >= 0; i--) {
        const item = combinedItems[i]
        if (item.role === "assistant" && typeof item.content === "string" && item.content.trim() === normalizedPrompt) {
          return item.id
        }
      }
    }

    for (let i = combinedItems.length - 1; i >= 0; i--) {
      const item = combinedItems[i]
      if (item.role === "assistant" && item.interactions && item.interactions.length > 0) {
        return item.id
      }
    }

    return null
  }, [combinedItems, state.currentTask?.status, waitingPrompt])

  useEffect(() => {
    messagesEndRef.current?.scrollIntoView?.({ behavior: "smooth" })
  }, [state.messages, state.steps, state.traceEvents])

  useEffect(() => {
    if (state.filePreview.isOpen) {
      setDagPreviewOpen(false)
    }
  }, [state.filePreview.isOpen])

  useEffect(() => {
    const handleFilePreviewEvent = (event: Event) => {
      const { filePath, fileName, allFiles, currentIndex } = (event as CustomEvent<any>).detail || {}
      if (!filePath) return
      if (Array.isArray(allFiles) && allFiles.length > 0) {
        openFilePreview(filePath, fileName, allFiles, typeof currentIndex === "number" ? currentIndex : 0)
      } else {
        openFilePreview(filePath, fileName)
      }
    }

    window.addEventListener("openFilePreview", handleFilePreviewEvent as EventListener)
    return () => window.removeEventListener("openFilePreview", handleFilePreviewEvent as EventListener)
  }, [openFilePreview])

  const handleDownload = async () => {
    try {
      if (!state.filePreview.fileId) return
      const response = await apiRequest(`${getApiUrl()}/api/files/download/${state.filePreview.fileId}`)
      if (!response.ok) {
        throw new Error(`Download failed: ${response.statusText}`)
      }

      const blob = await response.blob()
      const url = window.URL.createObjectURL(blob)
      const link = document.createElement("a")
      link.href = url
      link.download = state.filePreview.fileName || "download"
      document.body.appendChild(link)
      link.click()
      document.body.removeChild(link)
      window.URL.revokeObjectURL(url)
    } catch (error) {
      console.error("Failed to download file:", error)
    }
  }

  const handleMouseDown = useCallback((event: React.MouseEvent) => {
    event.preventDefault()
    setIsDragging(true)
  }, [])

  const handleMouseMove = useCallback((event: MouseEvent) => {
    if (!isDragging || !containerRef.current) return
    const containerRect = containerRef.current.getBoundingClientRect()
    const nextWidth = Math.min(80, Math.max(20, ((event.clientX - containerRect.left) / containerRect.width) * 100))
    setLeftWidth(nextWidth)
  }, [isDragging])

  const handleMouseUp = useCallback(() => {
    setIsDragging(false)
  }, [])

  useEffect(() => {
    if (!isDragging) {
      document.body.style.cursor = ""
      document.body.style.userSelect = ""
      return
    }

    document.body.style.cursor = "col-resize"
    document.body.style.userSelect = "none"
    window.addEventListener("mousemove", handleMouseMove, { passive: true })
    window.addEventListener("mouseup", handleMouseUp)

    return () => {
      window.removeEventListener("mousemove", handleMouseMove)
      window.removeEventListener("mouseup", handleMouseUp)
      document.body.style.cursor = ""
      document.body.style.userSelect = ""
    }
  }, [isDragging, handleMouseMove, handleMouseUp])

  const dagreGraph = new dagre.graphlib.Graph()
  dagreGraph.setGraph({
    rankdir: dagLayout === "LR" ? "LR" : "TB",
    nodesep: 80,
    ranksep: 100,
    marginx: 20,
    marginy: 20,
  })
  dagreGraph.setDefaultEdgeLabel(() => "")

  const validSteps = state.steps.filter((step: any) => step && typeof step.id === "string" && step.id.trim() !== "")
  validSteps.forEach((step: any, index: number) => {
    dagreGraph.setNode(step.id, {
      label: step.name || `Step ${index + 1}`,
      width: 250,
      height: 200,
    })
  })
  validSteps.forEach((step: any) => {
    if (!Array.isArray(step.dependencies)) return
    step.dependencies.forEach((depId: string) => {
      if (depId && validSteps.some((candidate: any) => candidate.id === depId)) {
        dagreGraph.setEdge(depId, step.id, {})
      }
    })
  })

  let dagreLayoutSuccessful = true
  try {
    dagre.layout(dagreGraph)
  } catch (error) {
    dagreLayoutSuccessful = false
    console.error("Dagre layout failed:", error)
  }

  const dagNodes = state.steps.map((step: any, index: number) => {
    const node = dagreLayoutSuccessful && step.id ? dagreGraph.node(step.id) : null
    const fallback = { x: (index % 3) * 300, y: Math.floor(index / 3) * 250 }
    const safeNode = typeof node === "object" && node !== null ? node : fallback
    return {
      id: step.id || `step-${index}`,
      type: "default",
      position: { x: (safeNode.x || 0) - 125, y: (safeNode.y || 0) - 100 },
      data: {
        label: step.name || `Step ${index + 1}`,
        status: step.status,
        description: step.description,
        tool_names: step.tool_names,
        started_at: step.started_at,
        completed_at: step.completed_at,
        result: step.result_data,
        conditional_branches: step.conditional_branches,
        required_branch: step.required_branch,
        is_conditional: step.is_conditional,
      },
    }
  })

  const validNodeIds = new Set(validSteps.map((step: any) => step.id))
  const dagEdges = dagreLayoutSuccessful
    ? validSteps.flatMap((step: any) => (
      Array.isArray(step.dependencies)
        ? step.dependencies
          .filter((depId: string) => validNodeIds.has(depId) && validNodeIds.has(step.id))
          .map((depId: string) => ({ id: `${depId}-${step.id}`, source: depId, target: step.id, data: {} }))
        : []
    ))
    : []

  const hasFinalAssistantMessage = combinedItems.length > 0 && combinedItems[combinedItems.length - 1].role === "assistant"
  const isPlanning = dagNodes.length === 0 && state.dagExecution?.phase === "planning"
  const hasError = dagNodes.length === 0 && (state.dagExecution?.phase === "failed" || state.currentTask?.status === "failed")
  const shouldShowHistoryLoading =
    combinedItems.length === 0 &&
    state.currentTask?.status !== "waiting_for_user" &&
    (state.isHistoryLoading || mode === "page")
  const shouldShowVirtualMessage =
    (state.isProcessing ||
      (state.traceEvents?.length || 0) > 0 ||
      state.currentTask?.status === "paused" ||
      state.currentTask?.status === "waiting_for_user") &&
    !hasFinalAssistantMessage

  return (
    <div
      ref={containerRef}
      className={cn(
        "h-full bg-background relative transition-all flex overflow-hidden",
        anyPreviewOpen ? "flex-row items-stretch" : "flex-col",
        mode === "embedded-preview" && "border-0",
        className
      )}
    >
      <div
        style={{ width: anyPreviewOpen ? `${leftWidth}%` : "100%" }}
        className={cn(anyPreviewOpen ? "" : "flex-1", "min-w-0 flex flex-col min-h-0 transition-[width] duration-0 relative")}
      >
        <div className="flex-1 overflow-y-auto">
          <main className={cn("mx-auto px-4 relative z-0 transition-all", mode === "page" ? "container max-w-4xl py-8" : "max-w-3xl py-4")}>
            <div className={cn(mode === "page" ? "space-y-6 pb-4" : "space-y-4 pb-4")}>
              {shouldShowHistoryLoading ? (
                <div className="flex flex-col items-center justify-center min-h-[40vh] py-16 text-center">
                  <div className="relative mb-6">
                    <div className="w-16 h-16 rounded-2xl bg-muted/30 flex items-center justify-center animate-pulse">
                      <Loader2 className="w-8 h-8 text-primary animate-spin" />
                    </div>
                  </div>
                  <h2 className="text-xl font-medium mb-2 text-foreground/80">
                    {state.isHistoryLoading ? t("common.loading") : t("builds.preview.initialMessage")}
                  </h2>
                </div>
              ) : (
                <>
                  {combinedItems.map((item) => (
                    <ChatMessage
                      key={item.id}
                      role={item.role}
                      content={item.content}
                      rawContent={item.rawContent}
                      traceEvents={item.traceEvents as any || []}
                      showProcessView={true}
                      timestamp={item.timestamp}
                      interactions={item.interactions}
                      interactionsActive={item.id === activeWaitingMessageId}
                    />
                  ))}

                  {shouldShowVirtualMessage && (
                    <ChatMessage
                      role="assistant"
                      content={state.currentTask?.status === "waiting_for_user" ? waitingPrompt : null}
                      traceEvents={state.traceEvents as any || []}
                      showProcessView={true}
                      isVirtual
                      taskStatus={state.currentTask?.status}
                      interactions={state.currentTask?.status === "waiting_for_user" ? waitingInteractions : undefined}
                      interactionsActive={state.currentTask?.status === "waiting_for_user"}
                    />
                  )}
                </>
              )}
              <div ref={messagesEndRef} />
            </div>
          </main>
        </div>

        <div className={cn("flex-shrink-0 z-10 glass", mode === "page" ? "pb-6" : "border-t bg-card/30 p-4")}>
          <div className={cn("mx-auto px-4", mode === "page" ? "container max-w-4xl" : "max-w-3xl px-0")}>
            {showTaskActions && (
              <div className="mb-4 flex flex-col gap-2 sm:flex-row sm:items-center sm:gap-3">
                <div className="flex flex-wrap items-center gap-2">
                  {showDagPreview && state.currentTask?.isDag !== false && (
                    <Button
                      type="button"
                      variant="outline"
                      className="h-auto rounded-xl bg-card/80 px-3 py-2 text-sm"
                      onClick={() => {
                        closeFilePreview()
                        setDagPreviewOpen(true)
                      }}
                      title={t("chatPage.executionPlan.title")}
                    >
                      <GitMerge className="w-3.5 h-3.5 mr-1" />
                      {t("chatPage.executionPlan.title")}
                    </Button>
                  )}

                  {showTaskFiles && (
                    <TaskFileManager taskId={state.taskId} onPreview={(fileId, fileName) => openFilePreview(fileId, fileName)}>
                      <Button type="button" variant="outline" className="h-auto rounded-xl bg-card/80 px-3 py-2 text-sm" title={t("files.header.title")}>
                        <FolderOpen className="w-3.5 h-3.5 mr-1" />
                        {t("files.header.title")}
                      </Button>
                    </TaskFileManager>
                  )}
                </div>

                {showTokenUsage && (
                  <div className="sm:ml-auto">
                    <TokenUsageDisplay taskId={state.taskId} isRunning={state.currentTask?.status === "running"} />
                  </div>
                )}
              </div>
            )}

            <ChatInput
              onSend={handleSend}
              isLoading={state.isProcessing}
              files={files}
              onFilesChange={setFiles}
              showModeToggle={false}
              hideConfig={mode === "embedded-preview"}
              taskStatus={state.currentTask?.status}
              onPause={pauseTask}
              onResume={resumeTask}
              taskConfig={state.currentTask ? {
                model: state.currentTask.modelId || state.currentTask.modelName,
                smallFastModel: state.currentTask.smallFastModelId,
                visualModel: state.currentTask.visualModelId,
                compactModel: state.currentTask.compactModelId,
                executionMode: state.currentTask.executionMode,
              } : undefined}
              readOnlyConfig={true}
              autoFocus={autoFocusInput}
            />
          </div>
        </div>
      </div>

      {anyPreviewOpen && (
        <div
          onMouseDown={handleMouseDown}
          className={cn("relative w-1 cursor-col-resize group z-[100] flex-shrink-0 hover:bg-primary/20 active:bg-primary/40 transition-colors", isDragging ? "bg-primary/40" : "bg-transparent")}
        >
          <div className="absolute inset-y-0 left-1/2 -translate-x-1/2 w-[1px] bg-border group-hover:bg-primary group-hover:w-[2px] transition-all" />
          <div className="absolute inset-y-0 -left-2 -right-2" />
        </div>
      )}

      {anyPreviewOpen && (
        <div
          style={{ width: `${100 - leftWidth}%`, pointerEvents: isDragging ? "none" : "auto" }}
          className="flex-shrink-0 px-2 py-6 overflow-hidden relative"
        >
          <PreviewSheet
            open={state.filePreview.isOpen || dagPreviewOpen}
            onOpenChange={(open) => {
              if (!open) {
                closeFilePreview()
                setDagPreviewOpen(false)
              }
            }}
            title={state.filePreview.isOpen ? <>{state.filePreview.fileName}</> : t("chatPage.executionPlan.title")}
            actions={state.filePreview.isOpen ? (
              <FilePreviewActionButtons
                viewMode={state.filePreview.viewMode}
                onViewModeChange={(mode) => dispatch({ type: "SET_FILE_PREVIEW_MODE", payload: mode })}
                fileName={state.filePreview.fileName || ""}
                onDownload={handleDownload}
                showText={true}
              />
            ) : null}
          >
            <div className="w-full h-full">
              {state.filePreview.isOpen ? (
                <FilePreviewContent open={state.filePreview.isOpen} />
              ) : (
                <CenterPanel
                  dagExecution={state.dagExecution}
                  dagNodes={dagNodes}
                  dagEdges={dagEdges as any}
                  dagLayout={dagLayout}
                  onLayoutChange={setDagLayout}
                  isPlanning={isPlanning}
                  hasError={hasError}
                  currentTaskStatus={state.currentTask?.status}
                  onRefresh={() => requestStatus()}
                  onFileClick={openFilePreview}
                />
              )}
            </div>
          </PreviewSheet>
        </div>
      )}

      {isDragging && <div className="fixed inset-0 z-[99] cursor-col-resize" />}
    </div>
  )
}
