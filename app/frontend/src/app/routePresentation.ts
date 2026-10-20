export function isExecutionDiscussionPath(pathname: string): boolean {
  return /^\/goals\/[^/]+\/(today|progress)$/.test(pathname)
}

