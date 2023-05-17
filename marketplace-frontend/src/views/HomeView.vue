<template>
  <div class="home">
    <Drawer />
    <SearchBar />
    <Usecase />
  </div>
</template>

<script>
import { defineComponent } from 'vue';
import Drawer from '@/components/Drawer.vue'
import SearchBar from '@/components/SearchBar.vue'
import Usecase from '@/components/Usecase.vue'
export default defineComponent({
  name: 'HomeView',
  components:{ Drawer, SearchBar, Usecase
  }

});
</script>
