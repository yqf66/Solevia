// materials: extracted without changing request or response semantics.
import type { GoalMaterial, MaterialAnalysis, MaterialDiff, MaterialReader, MaterialRevision, MaterialRoot, MaterialScan } from './contracts'
import { request } from './client'

export function listMaterialRoots(): Promise<MaterialRoot[]> {
  return request('/api/material-roots')
}

export function createMaterialRoot(displayName: string, path: string): Promise<MaterialRoot> {
  return request('/api/material-roots', {
    method: 'POST',
    body: JSON.stringify({ display_name: displayName, path }),
  })
}

export function listGoalMaterials(goalId: string): Promise<GoalMaterial[]> {
  return request(`/api/goals/${goalId}/materials`)
}

export function scanMaterialRoot(goalId: string, rootId: string): Promise<MaterialScan> {
  return request(`/api/goals/${goalId}/materials/roots/${rootId}/scan`)
}

export function importMaterialBatch(goalId: string, rootId: string, relativePaths: string[]): Promise<{ imported: GoalMaterial[]; errors: Array<{ relative_path: string; message: string }> }> {
  return request(`/api/goals/${goalId}/materials/local/batch`, {
    method: 'POST', body: JSON.stringify({ root_id: rootId, relative_paths: relativePaths }),
  })
}

export function linkLocalMaterial(
  goalId: string,
  payload: { root_id: string; relative_path: string; scope?: string; usage_policy?: string; pinned?: boolean },
): Promise<GoalMaterial> {
  return request(`/api/goals/${goalId}/materials/local`, {
    method: 'POST',
    body: JSON.stringify(payload),
  })
}

export function uploadMaterialFile(goalId: string, file: Blob, filename: string): Promise<GoalMaterial> {
  return request(`/api/goals/${goalId}/materials/upload?filename=${encodeURIComponent(filename)}`, {
    method: 'POST',
    body: file,
    headers: { 'Content-Type': file.type || 'application/octet-stream' },
  })
}

export function listMaterialRevisions(goalId: string, sourceId: string): Promise<MaterialRevision[]> { return request(`/api/goals/${goalId}/materials/${sourceId}/revisions`) }

export function readMaterial(goalId: string, sourceId: string, revisionId?: string, offset = 0): Promise<MaterialReader> { return request(`/api/goals/${goalId}/materials/${sourceId}/read?offset=${offset}${revisionId ? `&revision_id=${encodeURIComponent(revisionId)}` : ''}`) }

export function compareMaterialRevisions(goalId: string, sourceId: string, fromId: string, toId: string): Promise<MaterialDiff> { return request(`/api/goals/${goalId}/materials/${sourceId}/diff?from_revision_id=${encodeURIComponent(fromId)}&to_revision_id=${encodeURIComponent(toId)}`) }

export function indexMaterialSemantics(goalId: string, sourceId: string): Promise<{ status: string }> { return request(`/api/goals/${goalId}/materials/${sourceId}/semantic-index`, { method: 'POST' }) }

export function processLocalMaterial(goalId: string, sourceId: string, pdfParser?: 'AUTO' | 'BASIC' | 'MINERU'): Promise<GoalMaterial> {
    return request(`/api/goals/${goalId}/materials/${sourceId}/process${pdfParser ? `?pdf_parser=${pdfParser}` : ''}`, { method: 'POST' })
}

export function refreshLocalMaterial(goalId: string, sourceId: string): Promise<GoalMaterial> {
  return request(`/api/goals/${goalId}/materials/${sourceId}/refresh`, { method: 'POST' })
}

export function unlinkGoalMaterial(goalId: string, sourceId: string): Promise<void> {
  return request(`/api/goals/${goalId}/materials/${sourceId}`, { method: 'DELETE' })
}

export function listMaterialAnalyses(goalId: string): Promise<MaterialAnalysis[]> {
  return request(`/api/goals/${goalId}/materials/analyses`)
}

export function analyzeGoalMaterials(
  goalId: string,
  query: string,
  options: { maxChunks?: number; maxTotalChars?: number; sourceIds?: string[] } = {},
): Promise<MaterialAnalysis> {
  return request(`/api/goals/${goalId}/materials/analyze`, {
    method: 'POST',
    body: JSON.stringify({
      query,
      max_chunks: options.maxChunks ?? 8,
      max_total_chars: options.maxTotalChars ?? 12000,
      source_ids: options.sourceIds,
    }),
  })
}

export function confirmMaterialFindings(
  goalId: string,
  analysisId: string,
  findingIndexes: number[],
): Promise<MaterialAnalysis> {
  return request(`/api/goals/${goalId}/materials/analyses/${analysisId}/confirm`, {
    method: 'POST',
    body: JSON.stringify({ finding_indexes: findingIndexes }),
  })
}
