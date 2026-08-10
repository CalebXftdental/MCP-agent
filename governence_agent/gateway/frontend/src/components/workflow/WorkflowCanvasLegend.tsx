import './WorkflowCanvasLegend.css'

/**
 * A permanently-visible key for the canvas's port dots and warning marks —
 * the only other explanation of them is a browser tooltip on a 12px circle,
 * which nothing invites a first-time user to go find. Sits above the canvas
 * (not inside a hover reveal) so it's seen before the first drag is
 * attempted, not after one goes wrong.
 */
function WorkflowCanvasLegend() {
  return (
    <div className="wfc-legend">
      <span className="wfc-legend-item">
        <span className="wfc-legend-dot" aria-hidden="true" />
        Empty port — drag from an output dot (right side) onto this to connect
      </span>
      <span className="wfc-legend-item">
        <span className="wfc-legend-dot wfc-legend-dot--bound" aria-hidden="true" />
        Connected — drag it into empty space to disconnect
      </span>
      <span className="wfc-legend-item">
        <span className="wfc-legend-mark" aria-hidden="true">
          *
        </span>
        Required — still needs a value or a connection
      </span>
      <span className="wfc-legend-item">
        <span className="wfc-legend-mark wfc-legend-mark--ring" aria-hidden="true">
          !
        </span>
        Red outline — open the card, something on it needs fixing
      </span>
    </div>
  )
}

export default WorkflowCanvasLegend
