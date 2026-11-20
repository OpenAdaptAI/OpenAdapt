// Own closed refusal stubs; signatures follow the public SDK interfaces.
// Implementations are first-party; the SDK header itself is not vendored.
    HRESULT STDMETHODCALLTYPE SetFocus(void) override { return unexpected(); }
    HRESULT STDMETHODCALLTYPE GetRuntimeId(SAFEARRAY * *) override { return unexpected(); }
    HRESULT STDMETHODCALLTYPE FindFirst(enum TreeScope, IUIAutomationCondition *, IUIAutomationElement **) override { return unexpected(); }
    HRESULT STDMETHODCALLTYPE FindAll(enum TreeScope, IUIAutomationCondition *, IUIAutomationElementArray **) override { return unexpected(); }
    HRESULT STDMETHODCALLTYPE FindFirstBuildCache(enum TreeScope, IUIAutomationCondition *, IUIAutomationCacheRequest *, IUIAutomationElement **) override { return unexpected(); }
    HRESULT STDMETHODCALLTYPE FindAllBuildCache(enum TreeScope, IUIAutomationCondition *, IUIAutomationCacheRequest *, IUIAutomationElementArray **) override { return unexpected(); }
    HRESULT STDMETHODCALLTYPE BuildUpdatedCache(IUIAutomationCacheRequest *, IUIAutomationElement **) override { return unexpected(); }
    HRESULT STDMETHODCALLTYPE GetCurrentPropertyValue(PROPERTYID, VARIANT *) override { return unexpected(); }
    HRESULT STDMETHODCALLTYPE GetCurrentPropertyValueEx(PROPERTYID, BOOL, VARIANT *) override { return unexpected(); }
    HRESULT STDMETHODCALLTYPE GetCachedPropertyValue(PROPERTYID, VARIANT *) override { return unexpected(); }
    HRESULT STDMETHODCALLTYPE GetCachedPropertyValueEx(PROPERTYID, BOOL, VARIANT *) override { return unexpected(); }
    HRESULT STDMETHODCALLTYPE GetCachedPatternAs(PATTERNID, REFIID, void **) override { return unexpected(); }
    HRESULT STDMETHODCALLTYPE GetCurrentPattern(PATTERNID, IUnknown **) override { return unexpected(); }
    HRESULT STDMETHODCALLTYPE GetCachedPattern(PATTERNID, IUnknown **) override { return unexpected(); }
    HRESULT STDMETHODCALLTYPE GetCachedParent(IUIAutomationElement **) override { return unexpected(); }
    HRESULT STDMETHODCALLTYPE GetCachedChildren(IUIAutomationElementArray **) override { return unexpected(); }
    HRESULT STDMETHODCALLTYPE get_CurrentProcessId(int *) override { return unexpected(); }
    HRESULT STDMETHODCALLTYPE get_CurrentControlType(CONTROLTYPEID *) override { return unexpected(); }
    HRESULT STDMETHODCALLTYPE get_CurrentLocalizedControlType(BSTR *) override { return unexpected(); }
    HRESULT STDMETHODCALLTYPE get_CurrentName(BSTR *) override { return unexpected(); }
    HRESULT STDMETHODCALLTYPE get_CurrentAcceleratorKey(BSTR *) override { return unexpected(); }
    HRESULT STDMETHODCALLTYPE get_CurrentAccessKey(BSTR *) override { return unexpected(); }
    HRESULT STDMETHODCALLTYPE get_CurrentHasKeyboardFocus(BOOL *) override { return unexpected(); }
    HRESULT STDMETHODCALLTYPE get_CurrentIsKeyboardFocusable(BOOL *) override { return unexpected(); }
    HRESULT STDMETHODCALLTYPE get_CurrentIsEnabled(BOOL *) override { return unexpected(); }
    HRESULT STDMETHODCALLTYPE get_CurrentAutomationId(BSTR *) override { return unexpected(); }
    HRESULT STDMETHODCALLTYPE get_CurrentClassName(BSTR *) override { return unexpected(); }
    HRESULT STDMETHODCALLTYPE get_CurrentHelpText(BSTR *) override { return unexpected(); }
    HRESULT STDMETHODCALLTYPE get_CurrentCulture(int *) override { return unexpected(); }
    HRESULT STDMETHODCALLTYPE get_CurrentIsControlElement(BOOL *) override { return unexpected(); }
    HRESULT STDMETHODCALLTYPE get_CurrentIsContentElement(BOOL *) override { return unexpected(); }
    HRESULT STDMETHODCALLTYPE get_CurrentIsPassword(BOOL *) override { return unexpected(); }
    HRESULT STDMETHODCALLTYPE get_CurrentNativeWindowHandle(UIA_HWND *) override { return unexpected(); }
    HRESULT STDMETHODCALLTYPE get_CurrentItemType(BSTR *) override { return unexpected(); }
    HRESULT STDMETHODCALLTYPE get_CurrentIsOffscreen(BOOL *) override { return unexpected(); }
    HRESULT STDMETHODCALLTYPE get_CurrentOrientation(enum OrientationType *) override { return unexpected(); }
    HRESULT STDMETHODCALLTYPE get_CurrentFrameworkId(BSTR *) override { return unexpected(); }
    HRESULT STDMETHODCALLTYPE get_CurrentIsRequiredForForm(BOOL *) override { return unexpected(); }
    HRESULT STDMETHODCALLTYPE get_CurrentItemStatus(BSTR *) override { return unexpected(); }
    HRESULT STDMETHODCALLTYPE get_CurrentBoundingRectangle(RECT *) override { return unexpected(); }
    HRESULT STDMETHODCALLTYPE get_CurrentLabeledBy(IUIAutomationElement **) override { return unexpected(); }
    HRESULT STDMETHODCALLTYPE get_CurrentAriaRole(BSTR *) override { return unexpected(); }
    HRESULT STDMETHODCALLTYPE get_CurrentAriaProperties(BSTR *) override { return unexpected(); }
    HRESULT STDMETHODCALLTYPE get_CurrentIsDataValidForForm(BOOL *) override { return unexpected(); }
    HRESULT STDMETHODCALLTYPE get_CurrentControllerFor(IUIAutomationElementArray **) override { return unexpected(); }
    HRESULT STDMETHODCALLTYPE get_CurrentDescribedBy(IUIAutomationElementArray **) override { return unexpected(); }
    HRESULT STDMETHODCALLTYPE get_CurrentFlowsTo(IUIAutomationElementArray **) override { return unexpected(); }
    HRESULT STDMETHODCALLTYPE get_CurrentProviderDescription(BSTR *) override { return unexpected(); }
    HRESULT STDMETHODCALLTYPE get_CachedProcessId(int *) override { return unexpected(); }
    HRESULT STDMETHODCALLTYPE get_CachedControlType(CONTROLTYPEID *) override { return unexpected(); }
    HRESULT STDMETHODCALLTYPE get_CachedLocalizedControlType(BSTR *) override { return unexpected(); }
    HRESULT STDMETHODCALLTYPE get_CachedName(BSTR *) override { return unexpected(); }
    HRESULT STDMETHODCALLTYPE get_CachedAcceleratorKey(BSTR *) override { return unexpected(); }
    HRESULT STDMETHODCALLTYPE get_CachedAccessKey(BSTR *) override { return unexpected(); }
    HRESULT STDMETHODCALLTYPE get_CachedHasKeyboardFocus(BOOL *) override { return unexpected(); }
    HRESULT STDMETHODCALLTYPE get_CachedIsKeyboardFocusable(BOOL *) override { return unexpected(); }
    HRESULT STDMETHODCALLTYPE get_CachedIsEnabled(BOOL *) override { return unexpected(); }
    HRESULT STDMETHODCALLTYPE get_CachedAutomationId(BSTR *) override { return unexpected(); }
    HRESULT STDMETHODCALLTYPE get_CachedClassName(BSTR *) override { return unexpected(); }
    HRESULT STDMETHODCALLTYPE get_CachedHelpText(BSTR *) override { return unexpected(); }
    HRESULT STDMETHODCALLTYPE get_CachedCulture(int *) override { return unexpected(); }
    HRESULT STDMETHODCALLTYPE get_CachedIsControlElement(BOOL *) override { return unexpected(); }
    HRESULT STDMETHODCALLTYPE get_CachedIsContentElement(BOOL *) override { return unexpected(); }
    HRESULT STDMETHODCALLTYPE get_CachedIsPassword(BOOL *) override { return unexpected(); }
    HRESULT STDMETHODCALLTYPE get_CachedNativeWindowHandle(UIA_HWND *) override { return unexpected(); }
    HRESULT STDMETHODCALLTYPE get_CachedItemType(BSTR *) override { return unexpected(); }
    HRESULT STDMETHODCALLTYPE get_CachedIsOffscreen(BOOL *) override { return unexpected(); }
    HRESULT STDMETHODCALLTYPE get_CachedOrientation(enum OrientationType *) override { return unexpected(); }
    HRESULT STDMETHODCALLTYPE get_CachedFrameworkId(BSTR *) override { return unexpected(); }
    HRESULT STDMETHODCALLTYPE get_CachedIsRequiredForForm(BOOL *) override { return unexpected(); }
    HRESULT STDMETHODCALLTYPE get_CachedItemStatus(BSTR *) override { return unexpected(); }
    HRESULT STDMETHODCALLTYPE get_CachedBoundingRectangle(RECT *) override { return unexpected(); }
    HRESULT STDMETHODCALLTYPE get_CachedLabeledBy(IUIAutomationElement **) override { return unexpected(); }
    HRESULT STDMETHODCALLTYPE get_CachedAriaRole(BSTR *) override { return unexpected(); }
    HRESULT STDMETHODCALLTYPE get_CachedAriaProperties(BSTR *) override { return unexpected(); }
    HRESULT STDMETHODCALLTYPE get_CachedIsDataValidForForm(BOOL *) override { return unexpected(); }
    HRESULT STDMETHODCALLTYPE get_CachedControllerFor(IUIAutomationElementArray **) override { return unexpected(); }
    HRESULT STDMETHODCALLTYPE get_CachedDescribedBy(IUIAutomationElementArray **) override { return unexpected(); }
    HRESULT STDMETHODCALLTYPE get_CachedFlowsTo(IUIAutomationElementArray **) override { return unexpected(); }
    HRESULT STDMETHODCALLTYPE get_CachedProviderDescription(BSTR *) override { return unexpected(); }
    HRESULT STDMETHODCALLTYPE GetClickablePoint(POINT *, BOOL *) override { return unexpected(); }
