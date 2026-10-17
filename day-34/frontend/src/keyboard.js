export function canStart(session, project) {
  return Boolean(project && session.goal.trim() && !session.pending && !session.applying);
}
export function handleGoalKeydown(event, session, project) {
  if (event.key !== 'Enter' || event.metaKey || event.altKey) return false;
  if (event.isComposing || event.keyCode === 229 || event.repeat) return false;
  if (event.shiftKey) return false;
  event.preventDefault();
  if (session.pending || session.applying) return false;
  if (event.ctrlKey) {
    const textarea = event.currentTarget;
    if (!textarea || textarea.disabled) return false;
    const {value, selectionStart, selectionEnd, maxLength} = textarea;
    if (maxLength >= 0 && value.length - (selectionEnd - selectionStart) + 1 > maxLength) return false;
    textarea.setRangeText('\n', selectionStart, selectionEnd, 'end');
    const InputEvent = textarea.ownerDocument?.defaultView?.Event || Event;
    textarea.dispatchEvent(new InputEvent('input', {bubbles: true}));
    return true;
  }
  if (!canStart(session, project)) return false;
  session.start();
  return true;
}
export function handleEscape(event, session, focusGoal) {
  if (event.key !== 'Escape' || event.ctrlKey || event.metaKey || event.altKey || event.shiftKey || event.isComposing || event.repeat || session.pending || session.applying) return false;
  event.preventDefault();
  focusGoal();
  return true;
}
