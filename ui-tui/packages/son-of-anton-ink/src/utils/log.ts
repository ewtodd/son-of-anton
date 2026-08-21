export function logError(error: unknown): void {
  if (!process.env.SON_OF_ANTON_INK_DEBUG_ERRORS) {
    return
  }

  console.error(error)
}
