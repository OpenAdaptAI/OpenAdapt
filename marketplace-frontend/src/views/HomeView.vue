<template>
  <div class="home">
    <Drawer />
  </div>
</template>

<script>
import { defineComponent } from 'vue';
import Drawer from '@/components/Drawer.vue'

export default defineComponent({
  name: 'HomeView',
  components:{ Drawer
  }

});
</script>
