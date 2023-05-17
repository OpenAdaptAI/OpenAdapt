<template>
  <div class="home">
    <Carousel />
    <SearchBar />
    <Usecase />
  </div>
</template>

<script>
import { defineComponent } from 'vue';
import Carousel from '@/components/Carousel.vue'
import SearchBar from '@/components/SearchBar.vue'
import Usecase from '@/components/Usecase.vue'
export default defineComponent({
  name: 'HomeView',
  components:{ Carousel, SearchBar, Usecase
  }

});
</script>
