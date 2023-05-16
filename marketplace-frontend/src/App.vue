<template>
  <v-app :style="{background: $vuetify.theme.themes.dark.background}">
    <Header />
    <v-content>
      <router-view></router-view>
    </v-content>
  </v-app>
</template>

<script>
import Header from '@/components/Header.vue'

export default {
  name: 'App',
  components : { Header },
  data: () => ({
    //
  }),
}
</script>
