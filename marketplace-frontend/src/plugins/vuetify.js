// Styles
import 'vuetify/styles'
//import '@fortawesome/fontawesome-free/css/all.css'

// Vuetify
import { createVuetify } from 'vuetify'

export default createVuetify({
  // https://vuetifyjs.com/en/introduction/why-vuetify/#feature-guides
  icons:{
    iconfont: 'md' || 'fa',
  },
  theme:{
    themes:{
      dark: {
        background: '#19244c'
      }
    }
  }
}
);
