// Plain names for the steps of the referral program on the home page's program
// map. The compiled program's public view titles most steps generically
// ("Select an interface target"); a run of the same certified program
// (data/site/run-referral-certified.json, r3's report, 29 steps in order) says what each one did
// ("click 'Create New Patient'", "type <fname>"). This maps one onto the other,
// and checks that the run and the program agree on every step's action. Field
// names only; never a typed value.

const FIELD = {
    title: 'Title', fname: 'First name', lname: 'Last name', DOB: 'Date of birth', sex: 'Sex', street: 'Street',
    city: 'City', state_label: 'State', postal_code: 'Postal code', phone_home: 'Home phone', email: 'Email',
    country_code: 'Country',
}

/** A field's plain name: the referral form's own, else the field id in words ("postal_code" reads "Postal code"). */
export function fieldName(id) {
    if (FIELD[id]) return FIELD[id]
    const words = String(id).replace(/[_-]+/g, ' ').trim()
    return words.charAt(0).toUpperCase() + words.slice(1)
}

export function stepName(intent) {
    const typed = intent.match(/^type <(\w+)>$/)
    if (typed) return `Type ${fieldName(typed[1])}`
    if (intent === 'press Control+a') return 'Select the field’s text'
    if (intent === 'press Enter') return 'Press Enter'
    if (intent.startsWith('scroll')) return 'Scroll down'
    if (intent === 'click recorded visual target') return 'Click the next field'
    const click = intent.match(/^click '(.+)'$/)
    // "Unassigned" is a select list; the text reader sometimes reads it as "Jnassigned".
    if (click) return /nassigned/.test(click[1]) ? 'Open the list' : `Click ${click[1].replace('FirstName', 'First name')}`
    return intent.charAt(0).toUpperCase() + intent.slice(1)
}

const kindOf = (intent) =>
    intent.startsWith('type ') ? 'type' : intent.startsWith('press ') ? 'key' : intent.startsWith('scroll') ? 'scroll' : 'click'

// { nodeId: name } for every step, the loop and the program's end.
export function stepTitles(spec, run) {
    const actions = spec.nodes.filter((n) => n.kind === 'action')
    if (actions.length !== run.steps.length) throw new Error('the run and the program differ in step count')
    const titles = {}
    actions.forEach((n, i) => {
        const intent = run.steps[i].intent
        if (kindOf(intent) !== n.action) throw new Error(`step ${i + 1}: the run says ${intent}, the program says ${n.action}`)
        titles[n.id] = stepName(intent)
    })
    for (const n of spec.nodes) {
        if (n.kind === 'loop') titles[n.id] = 'For each referral in the list'
        if (n.kind === 'terminal') titles[n.id] = 'Every referral done'
    }
    return titles
}
