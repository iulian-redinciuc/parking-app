// Types for `@slot-editor` = tools/slot-editor/editor.js (a plain ES module shared with the
// standalone editor, P1.3). Only what the admin's SlotEditor (P7.3) uses is declared.
declare module '@slot-editor' {
  export type Point = [number, number]
  export type EditorMode = 'slots' | 'lines' | 'label'
  export type LineTarget = 'line_a' | 'line_b' | 'roi'
  export type InDirection = 'a_to_b' | 'b_to_a'

  export interface EditorSlot {
    id: string
    zone: string
    polygon: Point[]
    type: string
  }

  export interface EditorState {
    mode: EditorMode
    zone: string
    imageSize: [number, number] | null
    slots: EditorSlot[]
    selected: number
    draft: Point[]
    lineTarget: LineTarget
    lines: {
      roi: Point[] | null
      lineA: Point[] | null
      lineB: Point[] | null
      inDirection: InDirection
    }
    moveAll: boolean
  }

  export interface Editor {
    readonly state: EditorState
    loadImage(src: string | Blob, name?: string): Promise<void>
    setMode(mode: EditorMode): void
    setZone(zone: string): void
    setCameraId(id: string): void
    setMoveAll(on: boolean): void
    setLineTarget(target: LineTarget): void
    setInDirection(dir: InDirection): void
    selectedSlot(): EditorSlot | null
    updateSelected(fields: Partial<Pick<EditorSlot, 'id' | 'zone' | 'type'>>): void
    deleteSelected(): void
    duplicateSelected(dir?: 'right' | 'down'): void
    finish(): void
    cancel(): void
    undoPoint(): void
    fit(): void
    zoom(factor: number): void
    handleKey(e: KeyboardEvent): boolean
    slotFile(): Record<string, unknown>
    loadSlotFile(data: unknown): void
    lineFile(): Record<string, unknown>
    loadLineFile(data: unknown): void
    validate(): string[]
    destroy(): void
  }

  export interface EditorOptions {
    zone?: string
    cameraId?: string
    onChange?: (editor: Editor) => void
    onStatus?: (text: string) => void
  }

  export const SLOT_TYPES: readonly string[]
  export function createEditor(canvas: HTMLCanvasElement, opts?: EditorOptions): Editor
}
