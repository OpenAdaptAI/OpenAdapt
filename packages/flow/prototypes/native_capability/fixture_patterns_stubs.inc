// Own closed refusal stubs; signatures follow the public SDK interfaces.
// Implementations are first-party; the SDK header itself is not vendored.
    HRESULT STDMETHODCALLTYPE Toggle(void) override { return unexpected(); }
    HRESULT STDMETHODCALLTYPE get_CurrentToggleState(enum ToggleState *) override { return unexpected(); }
    HRESULT STDMETHODCALLTYPE get_CachedToggleState(enum ToggleState *) override { return unexpected(); }
    HRESULT STDMETHODCALLTYPE Select(void) override { return unexpected(); }
    HRESULT STDMETHODCALLTYPE AddToSelection(void) override { return unexpected(); }
    HRESULT STDMETHODCALLTYPE RemoveFromSelection(void) override { return unexpected(); }
    HRESULT STDMETHODCALLTYPE get_CurrentIsSelected(BOOL *) override { return unexpected(); }
    HRESULT STDMETHODCALLTYPE get_CurrentSelectionContainer(IUIAutomationElement **) override { return unexpected(); }
    HRESULT STDMETHODCALLTYPE get_CachedIsSelected(BOOL *) override { return unexpected(); }
    HRESULT STDMETHODCALLTYPE get_CachedSelectionContainer(IUIAutomationElement **) override { return unexpected(); }
    HRESULT STDMETHODCALLTYPE SetValue(BSTR) override { return unexpected(); }
    HRESULT STDMETHODCALLTYPE get_CurrentValue(BSTR *) override { return unexpected(); }
    HRESULT STDMETHODCALLTYPE get_CurrentIsReadOnly(BOOL *) override { return unexpected(); }
    HRESULT STDMETHODCALLTYPE get_CachedValue(BSTR *) override { return unexpected(); }
    HRESULT STDMETHODCALLTYPE get_CachedIsReadOnly(BOOL *) override { return unexpected(); }
