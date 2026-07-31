/**
 * Reusable UI kit for the governance console. Nothing here is mounted yet.
 *
 * Import from the barrel so the token stylesheet loads exactly once:
 *
 *   import { Card, DataTable, useToast } from '../components/ui'
 *
 * See README.md in this directory for what each piece replaces in
 * gateway/static/app.html, and for the mounting order when the React shell is
 * ready to take over.
 */

// Tokens first: components override individual properties, so their rules must
// come after the variable definitions in source order.
import './theme.css'

export { default as AddButton } from './AddButton'
export type { AddButtonProps } from './AddButton'

export { default as Avatar } from './Avatar'
export type { AvatarProps } from './Avatar'

export { default as Badge, SeverityBar } from './Badge'
export type { BadgeProps, SeverityBarProps } from './Badge'

// Colour scales live apart from the components that render them — see scales.ts.
export { severityTone, CATEGORICAL } from './scales'
export type { BadgeTone, Severity } from './scales'

export { default as BarList } from './BarList'
export type { BarItem, BarListProps } from './BarList'

export { default as Button } from './Button'
export type { ButtonProps, ButtonSize, ButtonVariant } from './Button'

export { default as Card, SectionHeader } from './Card'
export type { CardProps, SectionHeaderProps } from './Card'

export { default as Chip } from './Chip'
export type { ChipProps } from './Chip'

export { default as CollapseToggle } from './CollapseToggle'
export type { CollapseToggleProps } from './CollapseToggle'

export { default as CidrInput } from './CidrInput'
export type { CidrInputProps } from './CidrInput'

export { CodeBlock, SecretKey } from './CodeBlock'
export type { CodeBlockProps, SecretKeyProps } from './CodeBlock'

export { default as DataTable } from './DataTable'
export type { Column, DataTableProps } from './DataTable'

export { default as DateTimePicker } from './DateTimePicker'
export type { DateTimePickerProps } from './DateTimePicker'

export { default as Donut, Legend } from './Donut'
export type { DonutProps, DonutSegment, LegendProps } from './Donut'

export { default as Dropdown } from './Dropdown'
export type { DropdownOption, DropdownProps } from './Dropdown'

export { default as Drawer } from './Drawer'
export type { DrawerProps } from './Drawer'

export { default as EmptyState } from './EmptyState'
export type { EmptyStateProps } from './EmptyState'

export { default as Field, Input, Select, Textarea } from './Field'
export type { FieldProps, InputProps, SelectProps, TextareaProps } from './Field'

export { default as FileTypeIcon } from './FileTypeIcon'
export type { FileTypeIconProps } from './FileTypeIcon'

export { default as Heatmap } from './Heatmap'
export type { HeatmapProps } from './Heatmap'

export { default as KeyValue } from './KeyValue'
export type { KeyValueItem, KeyValueProps } from './KeyValue'

export { default as Modal } from './Modal'
export type { ModalProps } from './Modal'

export { default as PlusIcon } from './PlusIcon'
export type { PlusIconProps } from './PlusIcon'

export { default as SegmentedControl } from './SegmentedControl'
export type { Segment, SegmentedControlProps } from './SegmentedControl'

export { default as Skeleton, SkeletonText } from './Skeleton'
export type { SkeletonProps, SkeletonTextProps } from './Skeleton'

export { default as Sparkline } from './Sparkline'
export type { SparklineProps } from './Sparkline'

export { default as Spinner, TypingDots } from './Spinner'
export type { SpinnerProps, TypingDotsProps } from './Spinner'

export { default as StackedBars } from './StackedBars'
export type { StackedBarBucket, StackedBarSegment, StackedBarsProps } from './StackedBars'

export { default as Stat, StatGrid } from './Stat'
export type { StatGridProps, StatProps, StatTone } from './Stat'

export { default as Switch } from './Switch'
export type { SwitchProps } from './Switch'

export { default as TimePicker } from './TimePicker'
export type { TimePickerProps } from './TimePicker'

export { default as Tooltip } from './Tooltip'
export type { TooltipProps, TooltipSide } from './Tooltip'

export { default as ToastProvider } from './ToastProvider'
export type { ToastProviderProps } from './ToastProvider'
export { useToast } from './toast-context'
export type { Toast, ToastApi, ToastOptions, ToastTone } from './toast-context'

export { usePresence, useCountUp, useReducedMotion } from './motion'
export type { CountUpOptions, PresenceState } from './motion'
