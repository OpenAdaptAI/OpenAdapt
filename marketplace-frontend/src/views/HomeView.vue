<template>
  <div class="home">
    <Drawer />
    <SearchBar />
  </div>
</template>

<script>
import { defineComponent } from 'vue';
import Drawer from '@/components/Drawer.vue'
import SearchBar from '@/components/SearchBar.vue'

export default defineComponent({
  name: 'HomeView',
  components:{ Drawer, SearchBar
  }

});
</script>
